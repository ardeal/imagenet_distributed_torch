- sampler code to test distributed torch using imagenet dataset. 
- train script:
CUDA_VISIBLE_DEVICES=0,1,2,3 WORLD_SIZE=4 python -m torch.distributed.launch --nproc_per_node=4 --master_port=49611 torch_distributed_ddp_imagenet.py

- This code is copied from Nvidia apex examples:
    - https://github.com/NVIDIA/apex/tree/master/examples/imagenet




