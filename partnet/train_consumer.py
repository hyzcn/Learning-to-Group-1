#!/usr/bin/env python
"""Train point cloud instance segmentation models"""

import sys
import os
import os.path as osp

sys.path.insert(0, osp.dirname(__file__) + '/..')
import argparse
import logging
import time

import torch
from torch import nn

from core.config import purge_cfg
from core.solver.build import build_optimizer, build_scheduler
from core.nn.freezer import Freezer
from core.utils.checkpoint import Checkpointer
from core.utils.logger import setup_logger
from core.utils.metric_logger import MetricLogger
from core.utils.tensorboard_logger import TensorboardLogger
from core.utils.torch_util import set_random_seed

from partnet.models.build import build_model
from partnet.data.build import build_dataloader
from IPython import embed
import numpy as np

import shaper.models.pointnet2.functions as _F
import torch.nn.functional as F
from partnet.models.pn2 import PointNetCls
from core.nn.functional import cross_entropy
from core.nn.functional import focal_loss
from core.nn.functional import l2_loss

from subprocess import Popen


def parse_args():
    parser = argparse.ArgumentParser(description='PyTorch 3D Deep Learning Training')
    parser.add_argument(
        '--cfg',
        dest='config_file',
        default='',
        metavar='FILE',
        help='path to config file',
        type=str,
    )
    parser.add_argument(
        'opts',
        help='Modify config options using the command-line',
        default=None,
        nargs=argparse.REMAINDER,
    )

    args = parser.parse_args()
    return args

def mask_to_xyz(pc, index, sample_num=1024):
    #pc:1 x 3 x num_points
    #index:num x num_points
    pc = pc.squeeze(0)
    parts_num = index.shape[0]
    parts_xyz = torch.zeros([parts_num, 3, sample_num]).cuda().type(torch.FloatTensor)
    parts_mean = torch.zeros([parts_num, 3]).cuda().type(torch.FloatTensor)
    for i in range(parts_num):
        part_pc = torch.masked_select(pc.squeeze(),mask=index[i].unsqueeze(0).byte()).reshape(3,-1)
        length = part_pc.shape[1]
        if length == 0:
            continue
        parts_mean[i] = torch.mean(part_pc, 1)
        initial_index = np.random.randint(length)
        parts_xyz[i] = part_pc[:,initial_index].unsqueeze(1).expand_as(parts_xyz[i])
        cur_sample_num = length if length < sample_num else sample_num
        parts_xyz[i,:,:cur_sample_num] = part_pc[:,torch.randperm(length)[:cur_sample_num]]
    return parts_xyz.cuda(), parts_mean.cuda().unsqueeze(-1)

def tile(a, dim, n_tile):
    init_dim = a.size(dim)
    repeat_idx = [1] * a.dim()
    repeat_idx[dim] = n_tile
    a = a.repeat(*(repeat_idx))
    order_index = torch.LongTensor(np.concatenate([init_dim * np.arange(n_tile) + i for i in range(init_dim)])).cuda()
    return torch.index_select(a, dim, order_index)

policy_update_bs = 64
xyz_pool1 = torch.zeros([0,3,1024]).float()
xyz_pool2 = torch.zeros([0,3,1024]).float()
context_xyz_pool1 = torch.zeros([0,3,1024]).float()
context_xyz_pool2 = torch.zeros([0,3,1024]).float()
context_context_xyz_pool = torch.zeros([0,3,2048]).float()
context_label_pool = torch.zeros([0]).float()
context_purity_pool = torch.zeros([0]).float()
label_pool = torch.zeros([0]).float()
purity_purity_pool = torch.zeros([0]).float()
purity_xyz_pool = torch.zeros([0,3,1024]).float()
policy_purity_pool = torch.zeros([0,policy_update_bs]).float()
policy_reward_pool = torch.zeros([0,policy_update_bs]).float()
policy_xyz_pool1 = torch.zeros([0,policy_update_bs,3,1024]).float()
policy_xyz_pool2 = torch.zeros([0,policy_update_bs,3,1024]).float()
cur_buffer = 'start'
rbuffer = {}
count = 0

def train_one_epoch(
                    model_merge,
                    cur_epoch,
                    optimizer_embed,
                    output_dir_merge,
                    max_grad_norm=0.0,
                    freezer=None,
                    log_period=-1):
    global xyz_pool1
    global xyz_pool2
    global context_xyz_pool1
    global context_xyz_pool2
    global context_context_xyz_pool
    global context_label_pool
    global context_purity_pool
    global label_pool
    global purity_purity_pool
    global purity_xyz_pool
    global policy_purity_pool
    global policy_reward_pool
    global policy_xyz_pool1
    global policy_xyz_pool2
    global cur_buffer
    global rbuffer
    global count

    logger = logging.getLogger('shaper.train')
    meters = MetricLogger(delimiter='  ')

    softmax = nn.Softmax()
    end = time.time()
    model_merge.train()
    sys.stdout.flush()
    BS = policy_update_bs
    print('epoch: %d'%cur_epoch)
    
    #delete older models
    if cur_epoch >2:
        if (cur_epoch - 2) % 400 != 0:
            p = Popen('rm %s'%(os.path.join(output_dir_merge, 'model_%03d.pth'%(cur_epoch-2))), shell=True)

    #keep reading newest data generated by producer
    while True:
        buffer_txt = os.path.join(output_dir_merge, 'last_buffer')
        buffer_file = open(buffer_txt, 'r')
        new_buffer = buffer_file.read()
        buffer_file.close()

        if new_buffer != 'start':
           if cur_buffer != new_buffer:
                count = 0
                cur_buffer = new_buffer
                print('read data from %s'%cur_buffer)
                rbuffer = torch.load(os.path.join(output_dir_merge,'buffer','%s.pt'%cur_buffer))
                break
           count += 1
           if count <= 2:
               break
        time.sleep(10)

    #read data
    xyz_pool1 = rbuffer['xyz_pool1']
    xyz_pool2 = rbuffer['xyz_pool2']
    context_xyz_pool1 = rbuffer['context_xyz_pool1']
    context_xyz_pool2 = rbuffer['context_xyz_pool2']
    context_context_xyz_pool = rbuffer['context_context_xyz_pool']
    context_label_pool = rbuffer['context_label_pool']
    context_purity_pool = rbuffer['context_purity_pool']
    label_pool = rbuffer['label_pool']
    purity_purity_pool = rbuffer['purity_purity_pool']
    purity_xyz_pool = rbuffer['purity_xyz_pool']
    policy_purity_pool = rbuffer['policy_purity_pool']
    policy_reward_pool = rbuffer['policy_reward_pool']
    policy_xyz_pool1 = rbuffer['policy_xyz_pool1']
    policy_xyz_pool2 = rbuffer['policy_xyz_pool2']
    for i in range(20):
        bs2 = 64
        TRAIN_LEN = 1024
        UP_policy = 2048
        TRAIN_LEN_policy = 32
        bs_policy = int(128/policy_update_bs)

        #train binary branch
        cur_len = xyz_pool1.shape[0]
        cur_train_len = TRAIN_LEN if cur_len > TRAIN_LEN else cur_len
        perm_idx = torch.randperm(cur_len)
        logits1_all = torch.zeros([0]).type(torch.LongTensor).cuda()
        sub_xyz_pool1 = torch.index_select(xyz_pool1, dim=0, index=perm_idx[:cur_train_len])
        sub_xyz_pool2 = torch.index_select(xyz_pool2, dim=0, index=perm_idx[:cur_train_len])
        sub_label_pool = torch.index_select(label_pool, dim=0, index=perm_idx[:cur_train_len])
        perm_idx = torch.arange(cur_train_len)
        for i in range(int(cur_train_len/bs2)):
            optimizer_embed.zero_grad()
            part_xyz1 = torch.index_select(sub_xyz_pool1, dim=0, index=perm_idx[i*bs2:(i+1)*bs2]).cuda()
            part_xyz2 = torch.index_select(sub_xyz_pool2, dim=0, index=perm_idx[i*bs2:(i+1)*bs2]).cuda()
            siamese_label = torch.index_select(sub_label_pool, dim=0, index=perm_idx[i*bs2:(i+1)*bs2]).cuda()
            part_xyz = torch.cat([part_xyz1,part_xyz2],-1)
            part_xyz -= torch.mean(part_xyz,-1).unsqueeze(-1)
            part_xyz1 -= torch.mean(part_xyz1,-1).unsqueeze(-1)
            part_xyz2 -= torch.mean(part_xyz2,-1).unsqueeze(-1)
            part_xyz1 /=part_xyz1.norm(dim=1).max(dim=-1)[0].unsqueeze(-1).unsqueeze(-1)
            part_xyz2 /=part_xyz2.norm(dim=1).max(dim=-1)[0].unsqueeze(-1).unsqueeze(-1)
            part_xyz /=part_xyz.norm(dim=1).max(dim=-1)[0].unsqueeze(-1).unsqueeze(-1)
            logits1 = model_merge(part_xyz1,'backbone')
            logits2 = model_merge(part_xyz2,'backbone')
            merge_logits = model_merge(torch.cat([part_xyz, torch.cat([logits1.unsqueeze(-1).expand(-1,-1,part_xyz1.shape[-1]), logits2.unsqueeze(-1).expand(-1,-1,part_xyz2.shape[-1])], dim=-1)], dim=1), 'head')
            _, p = torch.max(merge_logits, 1)
            logits1_all = torch.cat([logits1_all, p],dim=0)
            merge_acc_arr = (p==siamese_label.long()).float()
            meters.update(meters_acc=torch.mean(merge_acc_arr))
            if torch.sum(siamese_label) != 0:
                merge_pos_acc = torch.mean(torch.index_select(merge_acc_arr,dim=0,index=(siamese_label==1).nonzero().squeeze()))
                meters.update(merge_pos_acc=merge_pos_acc)
            if torch.sum(1-siamese_label) != 0:
                merge_neg_acc = torch.mean(torch.index_select(merge_acc_arr, dim=0, index=(siamese_label==0).nonzero().squeeze()))
                meters.update(merge_neg_acc=merge_neg_acc)

            loss_sim = cross_entropy(merge_logits, siamese_label.long())

            loss_dict_embed = {
                'loss_sim': loss_sim,
            }
            meters.update(**loss_dict_embed)
            total_loss_embed = sum(loss_dict_embed.values())
            total_loss_embed.backward()
            optimizer_embed.step()

        #train context branch
        cur_len = context_xyz_pool1.shape[0]
        cur_train_len = TRAIN_LEN if cur_len > TRAIN_LEN else cur_len
        perm_idx = torch.randperm(cur_len)
        logits1_all = torch.zeros([0]).type(torch.LongTensor).cuda()
        sub_xyz_pool1 = torch.index_select(context_xyz_pool1, dim=0, index=perm_idx[:cur_train_len])
        sub_xyz_pool2 = torch.index_select(context_xyz_pool2, dim=0, index=perm_idx[:cur_train_len])
        sub_label_pool = torch.index_select(context_label_pool, dim=0, index=perm_idx[:cur_train_len])
        sub_context_context_xyz_pool = torch.index_select(context_context_xyz_pool, dim=0, index=perm_idx[:cur_train_len])
        perm_idx = torch.arange(cur_train_len)
        for i in range(int(cur_train_len/bs2)):
            optimizer_embed.zero_grad()
            part_xyz1 = torch.index_select(sub_xyz_pool1, dim=0, index=perm_idx[i*bs2:(i+1)*bs2]).cuda()
            part_xyz2 = torch.index_select(sub_xyz_pool2, dim=0, index=perm_idx[i*bs2:(i+1)*bs2]).cuda()
            siamese_label = torch.index_select(sub_label_pool, dim=0, index=perm_idx[i*bs2:(i+1)*bs2]).cuda()
            part_xyz = torch.cat([part_xyz1,part_xyz2],-1)
            part_xyz -= torch.mean(part_xyz,-1).unsqueeze(-1)
            part_xyz1 -= torch.mean(part_xyz1,-1).unsqueeze(-1)
            part_xyz2 -= torch.mean(part_xyz2,-1).unsqueeze(-1)
            part_xyz1 /=part_xyz1.norm(dim=1).max(dim=-1)[0].unsqueeze(-1).unsqueeze(-1)
            part_xyz2 /=part_xyz2.norm(dim=1).max(dim=-1)[0].unsqueeze(-1).unsqueeze(-1)
            part_xyz /=part_xyz.norm(dim=1).max(dim=-1)[0].unsqueeze(-1).unsqueeze(-1)
            logits1 = model_merge(part_xyz1,'backbone')
            logits2 = model_merge(part_xyz2,'backbone')
            context_xyz = torch.index_select(sub_context_context_xyz_pool, dim=0, index=perm_idx[i*bs2:(i+1)*bs2]).cuda()
            context_logits = model_merge(context_xyz,'backbone2')
            merge_logits = model_merge(torch.cat([part_xyz, torch.cat([logits1.detach().unsqueeze(-1).expand(-1,-1,part_xyz1.shape[-1]), logits2.detach().unsqueeze(-1).expand(-1,-1,part_xyz2.shape[-1])], dim=-1), torch.cat([context_logits.unsqueeze(-1).expand(-1,-1,part_xyz.shape[-1])], dim=-1)], dim=1), 'head2')
            _, p = torch.max(merge_logits, 1)
            logits1_all = torch.cat([logits1_all, p],dim=0)
            merge_acc_arr = (p==siamese_label.long()).float()
            meters.update(meters_acc_context=torch.mean(merge_acc_arr))
            if torch.sum(siamese_label) != 0:
                merge_pos_acc = torch.mean(torch.index_select(merge_acc_arr,dim=0,index=(siamese_label==1).nonzero().squeeze()))
                meters.update(merge_pos_acc_context=merge_pos_acc)
            if torch.sum(1-siamese_label) != 0:
                merge_neg_acc = torch.mean(torch.index_select(merge_acc_arr, dim=0, index=(siamese_label==0).nonzero().squeeze()))
                meters.update(merge_neg_acc_context=merge_neg_acc)

            loss_sim = cross_entropy(merge_logits, siamese_label.long())

            loss_dict_embed = {
                'loss_sim_context': loss_sim,
            }
            meters.update(**loss_dict_embed)
            total_loss_embed = sum(loss_dict_embed.values())
            total_loss_embed.backward()
            optimizer_embed.step()


        #train purity network
        cur_len = purity_purity_pool.shape[0]
        cur_train_len = TRAIN_LEN if cur_len > TRAIN_LEN else cur_len
        perm_idx = torch.randperm(cur_len)
        sub_purity_pool = torch.index_select(purity_purity_pool, dim=0, index=perm_idx[:cur_train_len])
        sub_purity_xyz_pool = torch.index_select(purity_xyz_pool, dim=0, index=perm_idx[:cur_train_len])
        perm_idx = torch.arange(cur_train_len)
        for i in range(int(cur_train_len/bs2)):
            optimizer_embed.zero_grad()
            part_xyz = torch.index_select(sub_purity_xyz_pool, dim=0, index=perm_idx[i*bs2:(i+1)*bs2]).cuda()
            logits_purity = model_merge(part_xyz, 'purity')
            siamese_label_l2= torch.index_select(sub_purity_pool, dim=0, index=perm_idx[i*bs2:(i+1)*bs2]).cuda()
            loss_purity = l2_loss(logits_purity.squeeze(), siamese_label_l2)
            loss_dict_embed = {
                'loss_purity2': loss_purity,
            }
            meters.update(**loss_dict_embed)
            total_loss_embed = sum(loss_dict_embed.values())
            total_loss_embed.backward()
            optimizer_embed.step()

        #train policy network
        cur_len = policy_xyz_pool1.shape[0]
        cur_train_len = TRAIN_LEN_policy if cur_len > TRAIN_LEN_policy else cur_len
        perm_idx = torch.randperm(cur_len)
        logits1_all = torch.zeros([0]).type(torch.LongTensor).cuda()
        sub_xyz_pool1 = torch.index_select(policy_xyz_pool1, dim=0, index=perm_idx[:cur_train_len])
        sub_xyz_pool2 = torch.index_select(policy_xyz_pool2, dim=0, index=perm_idx[:cur_train_len])
        sub_purity_pool = torch.index_select(policy_purity_pool, dim=0, index=perm_idx[:cur_train_len])
        sub_reward_pool = torch.index_select(policy_reward_pool, dim=0, index=perm_idx[:cur_train_len])
        perm_idx = torch.arange(cur_train_len)
        for i in range(int(cur_train_len/bs_policy)):
            optimizer_embed.zero_grad()
            part_xyz1 = torch.index_select(sub_xyz_pool1, dim=0, index=perm_idx[i*bs_policy:(i+1)*bs_policy]).cuda()
            part_xyz2 = torch.index_select(sub_xyz_pool2, dim=0, index=perm_idx[i*bs_policy:(i+1)*bs_policy]).cuda()
            purity_arr = torch.index_select(sub_purity_pool, dim=0, index=perm_idx[i*bs_policy:(i+1)*bs_policy]).cuda()
            reward_arr = torch.index_select(sub_reward_pool, dim=0, index=perm_idx[i*bs_policy:(i+1)*bs_policy]).cuda()
            logits11 = model_merge(part_xyz1.reshape([bs_policy*BS,3,1024]), 'policy')
            logits22 = model_merge(part_xyz2.reshape([bs_policy*BS,3,1024]), 'policy')
            policy_arr = model_merge(torch.cat([logits11, logits22],dim=-1), 'policy_head').squeeze()
            policy_arr = policy_arr.reshape([bs_policy, BS])
            score_arr = softmax(policy_arr*purity_arr)
            loss_policy = torch.mean(-torch.sum(score_arr*reward_arr, dim=1))
            meters.update(loss_policy=loss_policy)
            loss_policy.backward()
            optimizer_embed.step()

        if max_grad_norm > 0:
            nn.utils.clip_grad_norm_(model.parameters(), max_norm=max_grad_norm)

    train_time = time.time() - end
    meters.update(train_time=train_time)

    logger.info(
        meters.delimiter.join(
            [
                '{meters}',
                'lr_embed: {lr_embed:.4e}',
            ]
        ).format(
            meters=str(meters),
            lr_embed=optimizer_embed.param_groups[0]['lr'],
        )
    )
    meters.update(lr_embed=optimizer_embed.param_groups[0]['lr'])
    return meters

def train(cfg, output_dir='', output_dir_merge='', output_dir_refine=''):
    logger = logging.getLogger('shaper.train')

    # build model
    set_random_seed(cfg.RNG_SEED)

    model_merge = nn.DataParallel(PointNetCls(in_channels=3, out_channels=128)).cuda()

    # build optimizer
    cfg['SCHEDULER']['StepLR']['step_size']=150
    cfg['SCHEDULER']['MAX_EPOCH']=20000
    optimizer_embed = build_optimizer(cfg, model_merge)

    # build lr scheduler
    scheduler_embed = build_scheduler(cfg, optimizer_embed)
    checkpointer_embed = Checkpointer(model_merge,
                                optimizer=optimizer_embed,
                                scheduler=scheduler_embed,
                                save_dir=output_dir_merge,
                                logger=logger)
    checkpoint_data_embed = checkpointer_embed.load(cfg.MODEL.WEIGHT, resume=cfg.AUTO_RESUME, resume_states=cfg.RESUME_STATES)

    ckpt_period = cfg.TRAIN.CHECKPOINT_PERIOD

    # build data loader
    # Reset the random seed again in case the initialization of models changes the random state.
    set_random_seed(cfg.RNG_SEED)

    # build tensorboard logger (optionally by comment)
    tensorboard_logger = TensorboardLogger(output_dir_merge)

    # train
    max_epoch = cfg.SCHEDULER.MAX_EPOCH
    start_epoch = checkpoint_data_embed.get('epoch', 0)
    best_metric_name = 'best_{}'.format(cfg.TRAIN.VAL_METRIC)
    best_metric = checkpoint_data_embed.get(best_metric_name, None)
    logger.info('Start training from epoch {}'.format(start_epoch))
    for epoch in range(start_epoch, max_epoch):
        cur_epoch = epoch + 1
        scheduler_embed.step()
        start_time = time.time()
        train_meters = train_one_epoch(
                                       model_merge,
                                       cur_epoch,
                                       optimizer_embed=optimizer_embed,
                                       output_dir_merge = output_dir_merge,
                                       max_grad_norm=cfg.OPTIMIZER.MAX_GRAD_NORM,
                                       freezer=None,
                                       log_period=cfg.TRAIN.LOG_PERIOD,
                                       )
        epoch_time = time.time() - start_time
        logger.info('Epoch[{}]-Train {}  total_time: {:.2f}s'.format(
            cur_epoch, train_meters.summary_str, epoch_time))

        tensorboard_logger.add_scalars(train_meters.meters, cur_epoch, prefix='train')

        # checkpoint
        if (ckpt_period > 0 and cur_epoch % ckpt_period == 0) or cur_epoch == max_epoch:
            checkpoint_data_embed['epoch'] = cur_epoch
            checkpoint_data_embed[best_metric_name] = best_metric
            checkpointer_embed.save('model_{:03d}'.format(cur_epoch), **checkpoint_data_embed)

    return model

def main():
    args = parse_args()

    # load the configuration
    # import on-the-fly to avoid overwriting cfg
    from partnet.config.ins_seg_3d import cfg
    cfg.merge_from_file(args.config_file)
    cfg.merge_from_list(args.opts)
    purge_cfg(cfg)
    cfg.freeze()

    output_dir = cfg.OUTPUT_DIR
    # replace '@' with config path
    if output_dir:
        config_path = osp.splitext(args.config_file)[0]
        config_path = config_path.replace('configs', 'outputs')
        output_dir_merge = output_dir.replace('@', config_path)+'_merge'
        os.makedirs(output_dir_merge, exist_ok=True)
        output_dir = osp.join('outputs/stage1/', cfg.DATASET.PartNetInsSeg.TRAIN.stage1)
        os.makedirs(output_dir, exist_ok=True)

    logger = setup_logger('shaper', output_dir_merge, prefix='train')
    logger.info('Using {} GPUs'.format(torch.cuda.device_count()))
    logger.info(args)


    logger.info('Loaded configuration file {}'.format(args.config_file))
    logger.info('Running with config:\n{}'.format(cfg))

    assert cfg.TASK == 'ins_seg_3d'
    train(cfg, output_dir, output_dir_merge)


if __name__ == '__main__':
    main()
