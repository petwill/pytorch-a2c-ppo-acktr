#!/bin/bash -ex

export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/home/willy/.mujoco/mjpro150/bin

env=$1

network_size=64
scale_interval=1000000000000000
scale_threshold=2.
network_ratio=1.

adaptive_interval=1000
cdec=.9
cinc=8

tolerance=$2
max=.01

dir=$env-network_$network_size-network_ratio_$network_ratio-adaptive-$adaptive_interval-$cdec-$cinc-$tolerance-$max

python main.py --env-name "$env"  \
               --num-stack 1  \
               --hidden_size $network_size \
               --log-dir ./$dir \
               --scale-interval $scale_interval \
               --scale-threshold $scale_threshold \
               --plot-title  $dir \
               --saturation-log $dir/$dir.sat \
               --network-ratio $network_ratio \
               --adaptive-interval $adaptive_interval \
               --cdec $cdec \
               --cinc $cinc \
               --tolerance $tolerance \
               --log-interval 1000 --max-grad-norm-after $max
