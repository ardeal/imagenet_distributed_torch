import argparse
import os
import shutil
import time
import tqdm
from datetime import datetime
import logging

import numpy as np
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.optim
import torch.utils.data
import torch.utils.data.distributed
import torchvision.transforms as transforms
import torchvision.datasets as datasets
import torchvision.models as models
import torch.distributed as dist


import numpy as np
import torch
import torch.distributed as dist
import torchvision
import torchvision.transforms as transforms


from torch.utils.tensorboard import SummaryWriter

import apex
from apex.parallel import DistributedDataParallel as DDP
from apex.fp16_utils import *
from apex import amp, optimizers

_logger = logging.getLogger('Train')
# logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
_logger.setLevel(logging.INFO)


def fast_collate(batch, memory_format):

    imgs = [img[0] for img in batch]
    targets = torch.tensor([target[1] for target in batch], dtype=torch.int64)
    w = imgs[0].size[0]
    h = imgs[0].size[1]
    tensor = torch.zeros( (len(imgs), 3, h, w), dtype=torch.uint8).contiguous(memory_format=memory_format)
    for i, img in enumerate(imgs):
        nump_array = np.asarray(img, dtype=np.uint8)
        if nump_array.ndim < 3:
            nump_array = np.expand_dims(nump_array, axis=-1)
        nump_array = np.rollaxis(nump_array, 2)
        tensor[i] += torch.from_numpy(nump_array)
    return tensor, targets


def parse():
    model_names = sorted(name for name in models.__dict__ if name.islower() and not name.startswith("__") and callable(models.__dict__[name]))

    parser = argparse.ArgumentParser(description='PyTorch ImageNet Training')
    parser.add_argument('--data', metavar='DIR', default='/workspace/ssd2/DATA/imagenet', help='path to dataset')
    parser.add_argument('--arch', '-a', metavar='ARCH', default='resnet50', choices=model_names, help='model architecture:   | '.join(model_names) + ' (default: resnet50)')
    parser.add_argument('-j', '--workers', default=8, type=int, metavar='N', help='number of data loading workers (default: 4) these are different from the processes that run the programe. they are just for data loading')
    parser.add_argument('--epochs', default=2, type=int, metavar='N', help='number of total epochs to run')
    parser.add_argument('--start-epoch', default=0, type=int, metavar='N', help='manual epoch number (useful on restarts)')
    parser.add_argument('-b', '--batch-size', default=128, type=int, metavar='N', help='mini-batch size per GPU (default: 224) has to be a multiple of 8 to make use of Tensor Cores. for a GPU < 16 GB, max batch size is 224')
    parser.add_argument('--lr', '--learning-rate', default=0.1, type=float, metavar='LR', help='Initial learning rate.  Will be scaled by '
                             '<global batch size>/256: args.lr = args.lr* float(args.batch_size*args.world_size)/256. A warmup schedule will also be applied over the first 5 epochs.')
    parser.add_argument('--momentum', default=0.9, type=float, metavar='M', help='momentum')
    parser.add_argument('--weight-decay', '--wd', default=1e-4, type=float, metavar='W', help='weight decay (default: 1e-4)')
    parser.add_argument('--print-freq', '-p', default=50, type=int, metavar='N', help='print frequency (default: 10)')
    parser.add_argument('--resume', default='', type=str, metavar='PATH',  help='path to latest checkpoint (default: none)')
    parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true', help='evaluate model on validation set')
    parser.add_argument('--pretrained', dest='pretrained', action='store_true', help='use pre-trained model')

    parser.add_argument('--local_rank', default=0, type=int)
    parser.add_argument('--sync-bn', action='store_true', help='enabling apex sync BN.')

    parser.add_argument('--opt-level', type=str, default='O2')
    parser.add_argument('--keep-batchnorm-fp32', type=str, default=None)
    parser.add_argument('--loss-scale', type=str, default=None)
    parser.add_argument('--channels-last', type=bool, default=False)
    parser.add_argument('--train_split', type=str, default='train')
    parser.add_argument('--val_split', type=str, default='val')
    parser.add_argument('--pin_memory', type=bool, default='True')
    parser.add_argument('--input_image_size', type=int, default=224)

    args = parser.parse_args()

    return args

def main():
    global best_prec1, args

    args = parse()

    cudnn.benchmark = True
    best_prec1 = 0

    args.distributed = False
    if 'WORLD_SIZE' in os.environ:
        args.distributed = int(os.environ['WORLD_SIZE']) > 1

    args.gpu = 0
    args.world_size = 1

    if args.distributed:
        # this will be 0-3 if you have 4 GPUs on curr node
        args.gpu = args.local_rank
        torch.cuda.set_device(args.gpu)
        torch.distributed.init_process_group(backend='nccl', init_method='env://')
        # this is the total # of GPUs across all nodes   if using 2 nodes with 4 GPUs each, world size is 8
        args.world_size = torch.distributed.get_world_size()
    print("### global rank of curr node: {}".format(torch.distributed.get_rank()))

    assert torch.backends.cudnn.enabled, "Amp requires cudnn backend to be enabled."

    if args.local_rank==0:
        handler = logging.StreamHandler()
        formatter = logging.Formatter("%(asctime)s: %(message)s")
        handler.setFormatter(formatter)
        _logger.addHandler(handler)

        os.makedirs('runs', exist_ok=True)
        logfilename = 'runs/log_{}.txt'.format(datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
        handler = logging.FileHandler(logfilename)
        handler.setFormatter(formatter)
        _logger.addHandler(handler)
        _logger.info('------------> log file =={}'.format(logfilename))

    if args.local_rank==0:
        _logger.info("opt_level = {}".format(args.opt_level))
        _logger.info("keep_batchnorm_fp32 = {}, type == {}".format(args.keep_batchnorm_fp32, type(args.keep_batchnorm_fp32)))
        _logger.info("loss_scale = {}, type == {}".format(args.loss_scale, type(args.loss_scale)))
        _logger.info("\nCUDNN VERSION: {}\n".format(torch.backends.cudnn.version()))

    if args.channels_last:
        memory_format = torch.channels_last
    else:
        memory_format = torch.contiguous_format

    # create model
    if args.pretrained:
        _logger.info("=> using pre-trained model '{}'".format(args.arch))
        model = models.__dict__[args.arch](pretrained=True)
    else:
        _logger.info("=> creating model '{}'".format(args.arch))
        model = models.__dict__[args.arch]()

    if args.sync_bn:
        _logger.info("using apex synced BN")
        model = apex.parallel.convert_syncbn_model(model)

    model = model.cuda()

    # initialize tb logging, you don't want to "double log"
    # so only allow GPU0 to launch tb
    if torch.distributed.get_rank() == 0:
        writer = SummaryWriter(comment="_{}_gpux{}_b{}_cpu{}_opt{}".format(args.arch,
                                                                           args.world_size,
                                                                           args.batch_size,
                                                                           args.workers,
                                                                           args.opt_level))

    # Scale init learning rate based on global batch size
    args.lr = args.lr * float(args.batch_size*args.world_size)/256.
    optimizer = torch.optim.SGD(model.parameters(), args.lr,
                                momentum=args.momentum,
                                weight_decay=args.weight_decay)

    # Initialize Amp.  Amp accepts either values or strings for the optional override arguments,
    # for convenient interoperation with argparse.
    model, optimizer = amp.initialize(model, optimizer,
                                      opt_level=args.opt_level,
                                      keep_batchnorm_fp32=args.keep_batchnorm_fp32,
                                      loss_scale=args.loss_scale)

    # For distributed training, wrap the model with apex.parallel.DistributedDataParallel.
    # This must be done AFTER the call to amp.initialize.  If model = DDP(model) is called
    # before model, ... = amp.initialize(model, ...), the call to amp.initialize may alter
    # the types of model's parameters in a way that disrupts or destroys DDP's allreduce hooks.
    if args.distributed:
        # By default, apex.parallel.DistributedDataParallel overlaps communication with
        # computation in the backward pass.
        # model = DDP(model)
        # delay_allreduce delays all communication to the end of the backward pass.
        model = DDP(model, delay_allreduce=True)

    # define loss function (criterion) and optimizer
    criterion = nn.CrossEntropyLoss().cuda()

    # Optionally resume from a checkpoint
    if args.resume:
        # Use a local scope to avoid dangling references
        def resume():
            if os.path.isfile(args.resume):
                _logger.info("=> loading checkpoint '{}'".format(args.resume))
                checkpoint = torch.load(args.resume, map_location = lambda storage, loc: storage.cuda(args.gpu))
                args.start_epoch = checkpoint['epoch']
                best_prec1 = checkpoint['best_prec1']
                model.load_state_dict(checkpoint['state_dict'])
                optimizer.load_state_dict(checkpoint['optimizer'])
                _logger.info("=> loaded checkpoint '{}' (epoch {})".format(args.resume, checkpoint['epoch']))
            else:
                _logger.info("=> no checkpoint found at '{}'".format(args.resume))
        resume()

    # Data loading code
    traindir = os.path.join(args.data, 'train')
    valdir = os.path.join(args.data, 'val')

    if args.arch == "inception_v3":
        raise RuntimeError("Currently, inception_v3 is not supported by this example.")
    else:
        crop_size = 224
        val_size = 256

    # train_dataset = datasets.ImageFolder(
    #     traindir,
    #     transforms.Compose([
    #         transforms.RandomResizedCrop(crop_size),
    #         transforms.RandomHorizontalFlip(),
    #         # transforms.ToTensor(), Too slow
    #         # normalize,
    #     ]))
    # val_dataset = datasets.ImageFolder(valdir, transforms.Compose([
    #         transforms.Resize(val_size),
    #         transforms.CenterCrop(crop_size),
    #     ]))

    # # makes sure that each process gets a different slice of the training data
    # # during distributed training
    # train_sampler = None
    # val_sampler = None
    # if args.distributed:
    #     train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset)
    #     val_sampler = torch.utils.data.distributed.DistributedSampler(val_dataset)

    # collate_fn = lambda b: fast_collate(b, memory_format)

    # # notice we turn off shuffling and use distributed data sampler
    # train_loader = torch.utils.data.DataLoader(
    #     train_dataset, batch_size=args.batch_size, shuffle=(train_sampler is None),
    #     num_workers=args.workers, pin_memory=True, sampler=train_sampler, collate_fn=collate_fn)

    # val_loader = torch.utils.data.DataLoader(
    #     val_dataset,
    #     batch_size=args.batch_size, shuffle=False,
    #     num_workers=args.workers, pin_memory=True,
    #     sampler=val_sampler,
    #     collate_fn=collate_fn)
    
    train_loader = create_train_loader(args)
    val_loader = create_train_loader(args)

    _logger.info('validate before train')
    if args.evaluate:
        validate(val_loader, model, criterion)
        return

    if torch.distributed.get_rank() == 0:
        start_time = time.time()
    for epoch in range(args.start_epoch, args.epochs):
        # if args.distributed:
        #     train_sampler.set_epoch(epoch)

        # train for one epoch
        train_throughput, train_batch_time, train_losses, train_top1, train_top5, train_lr = train(train_loader, model, criterion, optimizer, epoch)

        # evaluate on validation set
        val_throughput, val_batch_time, val_losses, val_top1, val_top5 = validate(val_loader, model, criterion)

        # remember best prec@1 and save checkpoint
        # only allow GPU0 to print training states to prevent double logging
        if torch.distributed.get_rank() == 0:
            is_best = val_top1 > best_prec1
            best_prec1 = max(val_top1, best_prec1)
            save_checkpoint({
                'epoch': epoch + 1,
                'arch': args.arch,
                'state_dict': model.state_dict(),
                'best_prec1': best_prec1,
                'optimizer': optimizer.state_dict(),
            }, is_best, writer.log_dir)

            # log train and val states to tensorboard
            writer.add_scalar('Throughput/train', train_throughput, epoch + 1)
            writer.add_scalar('Throughput/val', val_throughput, epoch + 1)
            writer.add_scalar('Time/train', train_batch_time, epoch + 1)
            writer.add_scalar('Time/val', val_batch_time, epoch + 1)
            writer.add_scalar('Loss/train', train_losses, epoch + 1)
            writer.add_scalar('Loss/val', val_losses, epoch + 1)
            writer.add_scalar('Top1/train', train_top1, epoch + 1)
            writer.add_scalar('Top1/val', val_top1, epoch + 1)
            writer.add_scalar('Top5/train', train_top5, epoch + 1)
            writer.add_scalar('Top5/val', val_top5, epoch + 1)
            writer.add_scalar('Lr', train_lr, epoch + 1)

    # if torch.distributed.get_rank() == 0:
    if args.local_rank == 0:
        writer.close()
        time_elapse = time.time() - start_time
        mins, secs = divmod(time_elapse, 60)
        hrs, mins = divmod(mins, 60)
        _logger.info('### Training Time: {:.2f} hrs {:.2f} mins {:.2f} secs | {:.2f} secs'.format(hrs, mins, secs, time_elapse))
        _logger.info('### All Arguments:')
        _logger.info(args)
    
    dist.destroy_process_group()
    torch.cuda.empty_cache

    return


# class DataPrefetcher():
#     """
#     With Amp, it isn't necessary to manually convert data to half.
#     """
#     def __init__(self, loader):
#         print('aaaaaaaaaaaaaaaaaaaaaaaaaa')
#         self.loader = iter(loader)
#         self.stream = torch.cuda.Stream()
#         print('bbbbbbbbbbbbbbbbbbbbb')
#         self.mean = torch.tensor([0.485 * 255, 0.456 * 255, 0.406 * 255]).cuda().view(1,3,1,1)
#         print('cccccccccccccccc')
#         self.std = torch.tensor([0.229 * 255, 0.224 * 255, 0.225 * 255]).cuda().view(1,3,1,1)
#         print('dddddddddddddddddd')
#         self.preload()
#         print('eeeeeeeeeeeeeee')

#     def preload(self):
#         try:
#             self.next_input, self.next_target = next(self.loader)
#         except StopIteration:
#             self.next_input = None
#             self.next_target = None
#             return
#         # if record_stream() doesn't work, another option is to make sure device inputs are created
#         # on the main stream.
#         # self.next_input_gpu = torch.empty_like(self.next_input, device='cuda')
#         # self.next_target_gpu = torch.empty_like(self.next_target, device='cuda')
#         # Need to make sure the memory allocated for next_* is not still in use by the main stream
#         # at the time we start copying to next_*:
#         # self.stream.wait_stream(torch.cuda.current_stream())
#         with torch.cuda.stream(self.stream):
#             self.next_input = self.next_input.cuda(non_blocking=True)
#             self.next_target = self.next_target.cuda(non_blocking=True)
#             # more code for the alternative if record_stream() doesn't work:
#             # copy_ will record the use of the pinned source tensor in this side stream.
#             # self.next_input_gpu.copy_(self.next_input, non_blocking=True)
#             # self.next_target_gpu.copy_(self.next_target, non_blocking=True)
#             # self.next_input = self.next_input_gpu
#             # self.next_target = self.next_target_gpu

#             self.next_input = self.next_input.float()
#             self.next_input = self.next_input.sub_(self.mean).div_(self.std)

#     def next(self):
#         torch.cuda.current_stream().wait_stream(self.stream)
#         input = self.next_input
#         target = self.next_target
#         if input is not None:
#             input.record_stream(torch.cuda.current_stream())
#         if target is not None:
#             target.record_stream(torch.cuda.current_stream())
#         self.preload()
#         return input, target


def train(train_loader, model, criterion, optimizer, epoch):
    batch_time =  AverageMeter()
    loader_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()

    # switch to train mode
    model.train()
    end = time.time()
    # t_loader = time.time()

    # prefetcher = DataPrefetcher(train_loader)
    # input, target = prefetcher.next()
    i = 0

    # pbar = tqdm(train_loader, len(train_loader))
    _logger.info('len of train_loader == {}'.format(len(train_loader)))
    # while input is not None:
    for i, (input, target) in enumerate(train_loader):
        t0 = time.time()
        # i += 1
        curr_lr = adjust_learning_rate(optimizer, epoch, i, len(train_loader))

        # compute output
        input = input.cuda()
        target = target.cuda()
        output = model(input)
        loss = criterion(output, target)

        # compute gradient and do SGD step
        optimizer.zero_grad()

        # Mixed-precision training requires that the loss is scaled in order
        # to prevent the gradients from underflow
        with amp.scale_loss(loss, optimizer) as scaled_loss:
            scaled_loss.backward()

        optimizer.step()

        if i % args.print_freq == 0:
            # Every print_freq iterations, check the loss, accuracy, and speed.
            # For best performance, it doesn't make sense to print these metrics every
            # iteration, since they incur an allreduce and some host<->device syncs.

            # Measure accuracy
            prec1, prec5 = accuracy(output.data, target, topk=(1, 5))

            # Average across all global processes for logging
            if args.distributed:
                reduced_loss = reduce_tensor(loss.data)
                prec1 = reduce_tensor(prec1)
                prec5 = reduce_tensor(prec5)
            else:
                reduced_loss = loss.data

            # to_python_float incurs a host<->device sync
            losses.update(to_python_float(reduced_loss), input.size(0))
            top1.update(to_python_float(prec1), input.size(0))
            top5.update(to_python_float(prec5), input.size(0))

            torch.cuda.synchronize()
            batch_time.update((time.time() - end) / args.print_freq)
            end = time.time()
            if args.local_rank == 0:
                curr_throughput = args.world_size*args.batch_size/batch_time.val
                avg_throughput = args.world_size*args.batch_size/batch_time.avg
                _logger.info('Epoch: [{0}][{1}/{2}]\t'
                      'BatchTime {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                      'LoaderTime {loader_time.val:.3f} ({loader_time.avg:.3f})\t'
                      'Throughput {3:.3f} ({4:.3f})\t'
                      'Loss {loss.val:.10f} ({loss.avg:.4f})\t'
                      'Prec@1 {top1.val:.3f} ({top1.avg:.3f})\t'
                      'Prec@5 {top5.val:.3f} ({top5.avg:.3f})'.format(
                       epoch, i, len(train_loader),
                       curr_throughput, avg_throughput,
                       batch_time=batch_time,
                       loader_time=loader_time,
                       loss=losses,
                       top1=top1,
                       top5=top5))
        t_loader_0 = time.time()              
        # input, target = prefetcher.next()
        loader_time.update(time.time() - t_loader_0)
        # if i>=51: break
        

    # return training states for the curr epoch
    avg_throughput = args.world_size * args.batch_size / batch_time.avg
    return avg_throughput, batch_time.avg, losses.avg, top1.avg, top5.avg, curr_lr


def validate(val_loader, model, criterion):
    batch_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()

    # switch to evaluate mode
    model.eval()

    end = time.time()
    # print('1111111111111')
    # prefetcher = DataPrefetcher(val_loader)
    # print('22222222222')
    # input, target = prefetcher.next()
    # print('33333333333')


    i = 0
    for i, (input, target) in enumerate(val_loader):
    # while input is not None:
        i += 1
        input = input.cuda()
        target = target.cuda()
        # compute output
        with torch.no_grad():
            output = model(input)
            loss = criterion(output, target)
        # measure accuracy and record loss
        prec1, prec5 = accuracy(output.data, target, topk=(1, 5))
        if args.distributed:
            reduced_loss = reduce_tensor(loss.data)
            prec1 = reduce_tensor(prec1)
            prec5 = reduce_tensor(prec5)
        else:
            reduced_loss = loss.data

        losses.update(to_python_float(reduced_loss), input.size(0))
        top1.update(to_python_float(prec1), input.size(0))
        top5.update(to_python_float(prec5), input.size(0))

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        # TODO:  Change timings to mirror train().
        if args.local_rank == 0 and i % args.print_freq == 0:
            curr_throughput = args.world_size * args.batch_size / batch_time.val
            avg_throughput = args.world_size * args.batch_size / batch_time.avg
            _logger.info('Test: [{0}/{1}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Speed {2:.3f} ({3:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Prec@1 {top1.val:.3f} ({top1.avg:.3f})\t'
                  'Prec@5 {top5.val:.3f} ({top5.avg:.3f})'.format(
                   i, len(val_loader),
                   curr_throughput,
                   avg_throughput,
                   batch_time=batch_time,
                   loss=losses,
                   top1=top1,
                   top5=top5))
        # input, target = prefetcher.next()

    # return val states for the curr epoch
    avg_throughput = args.world_size * args.batch_size / batch_time.avg
    return avg_throughput, batch_time.avg, losses.avg, top1.avg, top5.avg


def save_checkpoint(state, is_best, out_path):
    if out_path:
        filename = os.path.join(out_path, 'checkpoint.pth.tar')
        bestfile = os.path.join(out_path, 'model_best.pth.tar')
    else:
        filename = 'checkpoint.pth.tar'
        bestfile = 'model_best.pth.tar'

    torch.save(state, filename)
    if is_best:
        shutil.copyfile(filename, bestfile)


class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def adjust_learning_rate(optimizer, epoch, step, len_epoch):
    """LR schedule that should yield 76% converged accuracy with batch size 256"""
    factor = epoch // 30

    if epoch >= 80:
        factor = factor + 1

    lr = args.lr*(0.1**factor)

    """Warmup"""
    if epoch < 5:
        lr = lr*float(1 + step + epoch*len_epoch)/(5.*len_epoch)

    for param_group in optimizer.param_groups:
        param_group['lr'] = lr

    return lr


def accuracy(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].contiguous().view(-1).float().sum(0, keepdim=True)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res


def reduce_tensor(tensor):
    rt = tensor.clone()
    dist.all_reduce(rt, op=dist.reduce_op.SUM)
    rt /= args.world_size
    return rt

def create_train_loader(args):
    """Constructs the train data loader for ILSVRC dataset."""
    traindir = os.path.join(args.data, args.train_split)
    trainset = torchvision.datasets.ImageFolder(
        root=traindir,
        transform=transforms.Compose(
            [
                transforms.RandomResizedCrop(args.input_image_size),
                transforms.RandomHorizontalFlip(),
                transforms.ToTensor(),
                transforms.Normalize(
                    mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]
                ),
            ]
        ),
    )
    # DistributedSampler
    train_sampler = torch.utils.data.distributed.DistributedSampler( trainset, shuffle=True)
    train_loader = torch.utils.data.DataLoader(
        trainset,
        batch_size=args.batch_size,
        num_workers=args.workers,
        pin_memory=args.pin_memory,
        sampler=train_sampler,
        drop_last=True,
    )
    return train_loader


def create_val_loader(args):
    """Constructs the validate data loader for ILSVRC dataset."""
    valdir = os.path.join(args.data, args.val_split)
    valset = torchvision.datasets.ImageFolder(
    root=valdir,
    transform=transforms.Compose(
        [
            transforms.Resize(args.input_image_size),
            transforms.CenterCrop(224),
            transforms.ToTensor(),
            transforms.Normalize(
                mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]
            ),
        ]
    ),
)
    val_sampler = torch.utils.data.distributed.DistributedSampler(valset)
    val_loader = torch.utils.data.DataLoader(
        valset,
        batch_size=args.batch_size,
        shuffle=False,
        sampler=val_sampler,
        num_workers=args.workers,
        pin_memory=args.pin_memory,
        drop_last=False,
    )
    return val_loader

if __name__ == '__main__':
    main()
