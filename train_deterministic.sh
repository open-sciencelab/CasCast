#!/bin/bash

gpus=4
node_num=1
single_gpus=`expr $gpus / $node_num`

cpus=13

# export NCCL_IB_DISABLE=1
# export NCCL_SOCKET_IFNAME=eth0
# export NCCL_DEBUG=INFO
# export NCCL_DEBUG_SUBSYS=ALL
# export TORCH_DISTRIBUTED_DEBUG=INFO

while true
do 
  PORT=$((((RANDOM<<15)|RANDOM)%49152 + 10000))
  break
done
echo $PORT

# export TORCH_DISTRIBUTED_DEBUG=DETAIL

srun -p ai4earth --kill-on-bad-exit=1 --quotatype=reserved --ntasks-per-node=$single_gpus --time=43200 --cpus-per-task=$cpus -N $node_num -o train_job/%j.out  --gres=gpu:$single_gpus --async  python -u train.py \
--init_method 'tcp://127.0.0.1:'$PORT \
-c ./configs/sevir/EarthFormer_xy.yaml \
--world_size $gpus \
--per_cpus $cpus \
--tensor_model_parallel_size 1 \
--outdir '/mnt/cache/gongjunchao/workdir/radar_forecasting/experiments' \
--desc  'sevir_earthformer_bs32_100k'

#
sleep 2
rm -f batchscript-*