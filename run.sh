#!/usr/bin/env bash

#python main.py PATH_OF_THE_CONFIG_FILE

export CUDA_DEVICE_ORDER=PCI_BUS_ID
export CUDA_VISIBLE_DEVICES=0
python3 main.py configs/condensenet_exp_0.json
