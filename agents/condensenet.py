import numpy as np

from tqdm import tqdm
import shutil

import torch
from torch import nn
from torch.backends import cudnn
from torch.autograd import Variable

from graphs.models.condensenet import CondenseNet
from graphs.losses.loss import CrossEntropyLoss2d
from datasets.cifar10 import Cifar10DataLoader
from graphs.weights_initializer import get_parameters

from tensorboardX import SummaryWriter
from utils.metrics import AverageMeter, AverageMeterList, evaluate
from utils.misc import print_cuda_statistics

cudnn.benchmark = True


class CondenseNetAgent:
    """
    This class will be responsible for handling the whole process of our architecture.
    """

    def __init__(self, config):
        self.config = config
        # Create an instance from the Model

        # Create an instance from the data loader
        self.data_loader = Cifar10DataLoader(self.config)
        # Create instance from the loss
        self.loss = CrossEntropyLoss2d()
        # Create instance from the optimizer
        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=self.config.learning_rate,
                                          weight_decay=self.config.weight_decay)

        # self.optimizer = torch.optim.SGD(self.model.parameters(),
        #                                  lr=self.config.learning_rate,
        #                                  momentum=self.config.momentum,weight_decay=self.config.weight_decay)
        # initialize my counters
        self.current_epoch = 0
        self.current_iteration = 0
        self.best_valid_mean_iou = 0

        # Check is cuda is available or not
        self.is_cuda = torch.cuda.is_available()
        # Construct the flag and make sure that cuda is available
        self.cuda = self.is_cuda & self.config.cuda

        # Set the seed of torch
        torch.manual_seed(self.config.seed)

        if self.cuda:
            print("Operation will be on *****GPU-CUDA***** ")
            torch.cuda.manual_seed_all(self.config.seed)
            print_cuda_statistics()

            # Get my models to run on CUDA
            self.vgg_model = self.vgg_model.cuda()
            self.model = self.model.cuda()
            self.loss = self.loss.cuda()
        else:
            print("Operation will be on *****CPU***** ")

        # Model Loading from the latest checkpoint if not found start from scratch.
        self.load_checkpoint(self.config.checkpoint_file)

        # Tensorboard Writer
        # TODO check graph visualization in tensorboardX (((DONE)))
        self.summary_writer = SummaryWriter(log_dir=self.config.summary_dir, comment='FCN8s')
        # dummy_input = Variable(torch.rand(1, 3, 224, 224))
        # self.summary_writer.add_graph(self.model, dummy_input)

    def save_checkpoint(self, filename='checkpoint.pth.tar', is_best=0):
        """
        Saving the latest checkpoint of the training
        :param filename: filename which will contain the state
        :param is_best: flag is it is the best model
        :return:
        """
        state = {
            'epoch': self.current_epoch + 1,
            'iteration': self.current_iteration,
            'state_dict': self.model.state_dict(),
            'optimizer': self.optimizer.state_dict(),
        }
        # Save the state
        torch.save(state, self.config.checkpoint_dir + filename)
        # If it is the best copy it to another file 'model_best.pth.tar'
        if is_best:
            shutil.copyfile(self.config.checkpoint_dir + filename,
                            self.config.checkpoint_dir + 'model_best.pth.tar')

    def load_checkpoint(self, filename):
        filename = self.config.checkpoint_dir + filename
        try:
            print("Loading checkpoint '{}'".format(filename))
            checkpoint = torch.load(filename)

            self.current_epoch = checkpoint['epoch']
            self.current_iteration = checkpoint['iteration']
            self.model.load_state_dict(checkpoint['state_dict'])
            self.optimizer.load_state_dict(checkpoint['optimizer'])

            print("Checkpoint loaded successfully from '{}' at (epoch {}) at (iteration {})\n"
                  .format(self.config.checkpoint_dir, checkpoint['epoch'], checkpoint['iteration']))
        except OSError as e:
            print("No checkpoint exists from '{}'. Skipping...".format(self.config.checkpoint_dir))
            print("**First time to train**")

    def run(self):
        """
        This function will the operator
        :return:
        """
        try:
            if self.config.mode == 'test':
                self.validate()
            else:
                self.train()

        except KeyboardInterrupt:
            print("You have entered CTRL+C.. Wait to finalize")

    def train(self):
        """
        Main training function, with per-epoch model saving
        """
        for epoch in range(self.current_epoch, self.config.max_epoch):
            self.current_epoch = epoch
            self.train_one_epoch()

            valid_mean_iou = self.validate()
            is_best = valid_mean_iou > self.best_valid_mean_iou
            if is_best:
                self.best_valid_mean_iou = valid_mean_iou
            self.save_checkpoint(is_best=is_best)

    def train_one_epoch(self):
        """
        One epoch training function
        """
        # Initialize tqdm
        tqdm_batch = tqdm(self.data_loader.train_loader, total=self.data_loader.train_iterations,
                          desc="Epoch-{}-".format(self.current_epoch))

        # Set the model to be in training mode (for batchnorm)
        self.vgg_model.train()
        self.model.train()
        # Initialize your average meters
        epoch_loss = AverageMeter()
        epoch_acc = AverageMeter()
        epoch_mean_iou = AverageMeter()
        epoch_iou_class = AverageMeterList(self.config.num_classes)

        for x, y in tqdm_batch:
            if self.cuda:
                x, y = x.cuda(async=self.config.async_loading), y.cuda(async=self.config.async_loading)

            x, y = Variable(x), Variable(y)
            # model
            pred = self.model(x)
            # loss
            cur_loss = self.loss(pred, y)
            if np.isnan(float(cur_loss.cpu().data[0])):
                raise ValueError('Loss is nan during training...')

            # optimizer
            self.optimizer.zero_grad()
            cur_loss.backward()
            self.optimizer.step()

            _, pred_max = torch.max(pred, 1)
            acc, _, mean_iou, iou, _ = evaluate(pred_max.cpu().data.numpy(), y.cpu().data.numpy(),
                                                self.config.num_classes)

            epoch_loss.update(cur_loss.data[0])
            epoch_acc.update(acc)
            epoch_mean_iou.update(mean_iou)
            epoch_iou_class.update(iou)

            self.current_iteration += 1

        self.summary_writer.add_scalar("epoch/loss", epoch_loss.val, self.current_iteration)
        self.summary_writer.add_scalar("epoch/accuracy", epoch_acc.val, self.current_iteration)
        self.summary_writer.add_scalar("epoch/mean_iou", epoch_mean_iou.val, self.current_iteration)
        tqdm_batch.close()

        print("Training at epoch-" + str(self.current_epoch) + " | " + "loss: " + str(
            epoch_loss.val) + " - acc-: " + str(
            epoch_acc.val) + "- mean_iou: " + str(epoch_mean_iou.val) + "- iou per class: " + str(
            epoch_iou_class.val))

    def validate(self):
        """
        One epoch validation
        :return:
        """
        tqdm_batch = tqdm(self.data_loader.valid_loader, total=self.data_loader.valid_iterations,
                          desc="Valiation at -{}-".format(self.current_epoch))

        # set the model in training mode
        self.vgg_model.eval()
        self.model.eval()

        epoch_loss = AverageMeter()
        epoch_acc = AverageMeter()
        epoch_mean_iou = AverageMeter()
        epoch_iou_class = AverageMeterList(self.config.num_classes)

        for x, y in tqdm_batch:
            if self.cuda:
                x, y = x.cuda(async=self.config.async_loading), y.cuda(async=self.config.async_loading)

            x, y = Variable(x), Variable(y)
            # model
            pred = self.model(x)
            # loss
            cur_loss = self.loss(pred, y)
            if np.isnan(float(cur_loss.data[0])):
                raise ValueError('Loss is nan during training...')

            _, pred_max = torch.max(pred, 1)
            acc, _, mean_iou, iou, _ = evaluate(pred_max.cpu().data.numpy(), y.cpu().data.numpy(),
                                                self.config.num_classes)

            epoch_loss.update(cur_loss.data[0])
            epoch_acc.update(acc)
            epoch_mean_iou.update(mean_iou)
            epoch_iou_class.update(iou)

        print("Validation Results at epoch-" + str(self.current_epoch) + " | " + "loss: " + str(
            epoch_loss.val) + " - acc-: " + str(
            epoch_acc.val) + "- mean_iou: " + str(epoch_mean_iou.val) + "- iou per class: " + str(
            epoch_iou_class.val))

        tqdm_batch.close()

        return epoch_mean_iou.val

    def finalize(self):
        """
        Finalize all the operations of the 2 Main classes of the process the operator and the data loader
        :return:
        """
        print("Please wait while finalizing the operation.. Thank you")
        self.save_checkpoint()
        self.summary_writer.export_scalars_to_json("{}all_scalars.json".format(self.config.summary_dir))
        self.summary_writer.close()
        self.data_loader.finalize()
