import importlib
import numpy as np
import os
import shutil
import time
import torch
import torch.distributions.beta as beta
import torch.nn as nn
import torch.optim as optim
from torch.optim.lr_scheduler import StepLR
from torch.utils.data import DataLoader
from tensorboardX import SummaryWriter

from segmentation3d.core.seg_infer import segmentation
from segmentation3d.core.seg_eval import evaluation
from segmentation3d.dataloader.dataset import SegmentationDataset
from segmentation3d.dataloader.sampler import EpochConcateSampler
from segmentation3d.loss.focal_loss import FocalLoss
from segmentation3d.loss.multi_dice_loss import MultiDiceLoss
from segmentation3d.loss.cross_entropy_loss import CrossEntropyLoss
from segmentation3d.loss.entropy_minization import EntropyMinimizationLoss
from segmentation3d.utils.file_io import load_config, setup_logger
from segmentation3d.utils.image_tools import save_intermediate_results
from segmentation3d.utils.model_io import load_checkpoint, save_checkpoint, delete_checkpoint


def get_data_loader(train_cfg):
    """
    Get data loader
    """
    dataset = SegmentationDataset(
                imlist_file=train_cfg.general.imseg_list_train,
                num_classes=train_cfg.dataset.num_classes,
                spacing=train_cfg.dataset.spacing,
                crop_size=train_cfg.dataset.crop_size,
                sampling_method=train_cfg.dataset.sampling_method,
                random_translation=train_cfg.dataset.random_translation,
                random_scale=train_cfg.dataset.random_scale,
                interpolation=train_cfg.dataset.interpolation,
                crop_normalizers=train_cfg.dataset.crop_normalizers)

    sampler = EpochConcateSampler(dataset, train_cfg.train.epochs)
    data_loader = DataLoader(dataset, sampler=sampler, batch_size=train_cfg.train.batchsize,
                             num_workers=train_cfg.train.num_threads, pin_memory=True)

    dataset_m = SegmentationDataset(
            imlist_file=train_cfg.general.imseg_list_train_ul,
            num_classes=train_cfg.dataset.num_classes,
            spacing=train_cfg.dataset.spacing,
            crop_size=train_cfg.dataset.crop_size,
            sampling_method=train_cfg.dataset.sampling_method,
            random_translation=train_cfg.dataset.random_translation,
            random_scale=train_cfg.dataset.random_scale,
            interpolation=train_cfg.dataset.interpolation,
            crop_normalizers=train_cfg.dataset.crop_normalizers)

    sampler_m = EpochConcateSampler(dataset_m, train_cfg.train.epochs)
    data_loader_m = DataLoader(dataset_m, sampler=sampler_m, batch_size=train_cfg.train.batchsize,
                               num_workers=train_cfg.train.num_threads, pin_memory=True)

    return data_loader, data_loader_m


def train_one_epoch(net, data_loader, data_loader_m, loss_funces, opt, logger, epoch_idx, use_gpu=True, use_mixup=False,
                    mixup_alpha=-1, use_ul=False, debug=False, model_folder=''):
    """
    """
    data_iter = iter(data_loader)
    if use_ul: data_iter_m = iter(data_loader_m)

    for batch_idx in range(len(data_loader.dataset)):
        begin_t = time.time()

        crops, masks, frames, filenames = data_iter.next()
        if use_gpu: crops, masks = crops.cuda(), masks.cuda()

        if use_ul:
            crops_m, masks_m, _, _ = data_iter_m.next()

            if use_gpu:
                crops_m = crops_m.cuda()
                masks_m = masks_m.cuda()

        # clear previous gradients
        opt.zero_grad()

        # network forward and backward
        noise = torch.zeros_like(crops).uniform_(-0.3, 0.3)
        if use_gpu: noise = noise.cuda()

        outputs = net(crops + noise)

        if use_ul:
            noise = torch.zeros_like(crops_m).uniform_(-0.3, 0.3)
            if use_gpu: noise = noise.cuda()

            outputs_m = net(crops_m + noise)

            outputs_m_avg = torch.zeros_like(outputs_m)
            if use_gpu: outputs_m_avg = outputs_m_avg.cuda()

            with torch.no_grad():
                num_iter = 5
                for idx in range(num_iter):
                    noise = torch.zeros_like(crops_m).uniform_(-0.3, 0.3)
                    if use_gpu: noise = noise.cuda()
                    outputs_m_avg += net(crops_m + noise)
                outputs_m_avg /= num_iter

            # get pseudo label
            _, pseudo_mask = outputs_m_avg.max(dim=1)
            pseudo_label = torch.unsqueeze(pseudo_mask, dim=1)

            # weakly supervised spatial regularization
            valid_idx = pseudo_label > 0
            pseudo_label = pseudo_label * (masks_m > 0)

            outputs_m_valid = outputs_m[:, :, valid_idx[0, 0, :]]
            pseudo_label_valid = pseudo_label[:, :, valid_idx[0, 0, :]]
            if pseudo_label_valid.nelement() > 0:
                outputs = outputs.view(outputs.shape[0], outputs.shape[1], -1)
                masks = masks.view(masks.shape[0], masks.shape[1], -1)

                outputs = torch.cat([outputs, outputs_m_valid], dim=2)
                masks = torch.cat([masks.long(), pseudo_label_valid], dim=2)

        # statistics of the masks
        num_pos_samples = (masks > 0).sum()
        num_neg_samples = masks.nelement() - num_pos_samples.item()
        msg = 'Pos samples: {}, Neg samples: {}'.format(num_pos_samples, num_neg_samples)
        logger.info(msg)

        train_loss = sum([loss_func(outputs, masks) for loss_func in loss_funces])
        train_loss.backward()

        # update weights
        opt.step()

        # save training crops for visualization
        if debug:
            batch_size = crops.size(0)
            save_intermediate_results(list(range(batch_size)), crops, masks, outputs, frames, filenames,
                                      os.path.join(model_folder, 'batch_{}'.format(batch_idx)))

            if use_ul:
                save_intermediate_results(list(range(batch_size)), crops_m, masks_m, outputs_m, frames, filenames,
                                          os.path.join(model_folder, 'ul_batch_{}'.format(batch_idx)))

                if pseudo_label.dim() == 4:
                    pseudo_label = torch.unsqueeze(pseudo_label, dim=1)
                save_intermediate_results(list(range(batch_size)), crops_m, pseudo_label, outputs_m, frames, filenames,
                                          os.path.join(model_folder, 'ul_pseudo_batch_{}'.format(batch_idx)))

        batch_duration = time.time() - begin_t

        # print training loss per batch
        msg = 'epoch: {}, batch: {}, train_loss: {:.4f}, time: {:.4f} s/vol'
        msg = msg.format(epoch_idx, batch_idx, train_loss.item(), batch_duration)

        logger.info(msg)


def train(train_config_file, infer_config_file, infer_gpu_id):
    """ Medical image segmentation training engine
    :param train_config_file: the input configuration file
    :return: None
    """
    assert os.path.isfile(train_config_file), 'Config not found: {}'.format(train_config_file)

    # load config file
    train_cfg = load_config(train_config_file)
    infer_cfg = load_config(infer_config_file)

    # get training parameters
    use_ul = train_cfg.train.use_ul
    use_debug = train_cfg.debug.save_inputs
    use_gpu = train_cfg.general.num_gpus > 0
    use_mixup = train_cfg.dataset.mixup_alpha >= 0
    mixup_alpha = train_cfg.dataset.mixup_alpha

    # clean the existing folder if training from scratch
    model_folder = os.path.join(train_cfg.general.save_dir, train_cfg.general.model_scale)
    if os.path.isdir(model_folder):
        if train_cfg.general.resume_epoch < 0:
            shutil.rmtree(model_folder)
            os.makedirs(model_folder)
    else:
        os.makedirs(model_folder)

    # copy training and inference config files to the model folder
    shutil.copy(train_config_file, os.path.join(model_folder, 'train_config.py'))
    shutil.copy(infer_config_file, os.path.join(train_cfg.general.save_dir, 'infer_config.py'))

    # enable logging
    log_file = os.path.join(model_folder, 'train_log.txt')
    logger = setup_logger(log_file, 'seg3d')

    # control randomness during training
    np.random.seed(train_cfg.general.seed)
    torch.manual_seed(train_cfg.general.seed)
    if use_gpu: torch.cuda.manual_seed(train_cfg.general.seed)

    # dataset
    data_loader, data_loader_m = get_data_loader(train_cfg)
    net_module = importlib.import_module('segmentation3d.network.' + train_cfg.net.name)
    net = net_module.SegmentationNet(1, train_cfg.dataset.num_classes)
    max_stride = net.max_stride()
    net_module.parameters_kaiming_init(net)
    if use_gpu: net = nn.parallel.DataParallel(net, device_ids=list(range(train_cfg.general.num_gpus))).cuda()
    assert np.all(np.array(train_cfg.dataset.crop_size) % max_stride == 0), 'crop size not divisible by max stride'

    # training optimizer
    opt = optim.Adam(net.parameters(), lr=train_cfg.train.lr, betas=train_cfg.train.betas)
    scheduler = StepLR(opt, step_size=train_cfg.train.step_size, gamma=0.5)

    # load checkpoint if resume epoch > 0
    if train_cfg.general.resume_epoch >= 0:
        last_save_epoch, batch_start = load_checkpoint(train_cfg.general.resume_epoch, net, opt, model_folder)
    else:
        last_save_epoch, batch_start = 0, 0

    focal_loss_func = FocalLoss(class_num=train_cfg.dataset.num_classes, alpha=train_cfg.loss.obj_weight,
                              gamma=train_cfg.loss.focal_gamma, use_gpu=use_gpu)
    dice_loss_func = MultiDiceLoss(weights=train_cfg.loss.obj_weight, num_class=train_cfg.dataset.num_classes,
                                  use_gpu=use_gpu)
    ce_loss_func = CrossEntropyLoss(weights=train_cfg.loss.obj_weight, use_gpu=use_gpu)

    loss_funces = []
    for loss_name in train_cfg.loss.name:
        if loss_name == 'Focal':
            loss_funces.append(focal_loss_func)
        elif loss_name == 'Dice':
            loss_funces.append(dice_loss_func)
        elif loss_name == 'CE':
            loss_funces.append(ce_loss_func)
        else:
            raise ValueError('Unsupported loss name.')

    writer = SummaryWriter(os.path.join(model_folder, 'tensorboard'))

    # loop over batches
    best_epoch, best_dsc_mean, best_dsc_std = last_save_epoch, 0.0, 0
    for epoch_idx in range(last_save_epoch + 1, train_cfg.train.epochs + 1):
        train_one_epoch(net, data_loader, data_loader_m, loss_funces, opt, logger, epoch_idx, use_gpu,
                        use_mixup, mixup_alpha, use_ul, use_debug, train_cfg.general.save_dir)

        scheduler.step()

        # inference
        if epoch_idx % train_cfg.train.save_epochs == 0:
            save_checkpoint(net, opt, epoch_idx, 0, train_cfg, max_stride, 1)
            seg_folder = os.path.join(train_cfg.general.save_dir, 'results')
            segmentation(train_cfg.general.imseg_list_val, train_cfg.general.save_dir, seg_folder, 'seg.nii.gz',
                         infer_gpu_id, False, True, False, False)

            # test DSC
            labels = [idx for idx in range(1, train_cfg.dataset.num_classes)]
            mean, std = evaluation(train_cfg.general.imseg_list_val, seg_folder, 'seg.nii.gz', labels, 10, None)

            if mean <= best_dsc_mean: delete_checkpoint(epoch_idx, train_cfg)
            else:
                best_dsc_mean = mean
                best_dsc_std = std
                best_epoch = epoch_idx

            msg = 'Best epoch {}, mean (std) = {:.4f} ({:.4f}), current epoch {} mean (std) = {:.4f} ({:.4f})'.format(
                best_epoch, best_dsc_mean, best_dsc_std, epoch_idx, mean, std)
            logger.info(msg)

    writer.close()
