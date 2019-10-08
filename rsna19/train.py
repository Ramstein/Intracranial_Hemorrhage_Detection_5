import argparse
import collections
import os

import numpy as np
import torch
import torch.optim as optim
from torch.optim import lr_scheduler
from torch.utils.data import DataLoader
from torchvision import datasets, models, transforms
from tqdm import tqdm
from data import dataset
import albumentations
import albumentations.pytorch

import torch.nn as nn
import torch.nn.functional as F
import config
import radam
from experiments import MODELS
from torch.utils.tensorboard import SummaryWriter


def build_model_str(model_name, fold, run):
    run_str = '' if not run else f'_{run}'
    fold_str = '' if fold == -1 else f'_fold_{fold}'

    return f'{model_name}{run_str}{fold_str}'


def train(model_name, fold, run=None, resume_epoch=-1):
    model_str = build_model_str(model_name, fold, run)

    model_info = MODELS[model_name]

    checkpoints_dir = f'{config.config.checkpoints_dir}/{model_str}'
    tensorboard_dir = f'{config.config.tensorboard_dir}/{model_str}'
    oof_dir = f'{config.config.oof_dir}/{model_str}'
    os.makedirs(checkpoints_dir, exist_ok=True)
    os.makedirs(tensorboard_dir, exist_ok=True)
    os.makedirs(oof_dir, exist_ok=True)
    print('\n', model_name, '\n')

    logger = SummaryWriter(log_dir=tensorboard_dir)

    model = model_info.factory(**model_info.args)
    model = model.cuda()

    # try:
    #     torchsummary.summary(model, (4, 512, 512))
    #     print('\n', model_name, '\n')
    # except:
    #     raise
    #     pass

    model = torch.nn.DataParallel(model).cuda()
    model = model.cuda()

    dataset_train = dataset.IntracranialDataset(
        csv_file='5fold.csv',
        folds=[f for f in range(config.config.nb_folds) if f != fold],
        preprocess_func=torch.from_numpy,
        # preprocess_func=albumentations.Compose([
        #                     albumentations.ShiftScaleRotate(),
        #                     albumentations.pytorch.ToTensorV2()
        #                 ]),
        **model_info.dataset_args
    )

    dataset_valid = dataset.IntracranialDataset(
        csv_file='5fold.csv',
        folds=[fold],
        # preprocess_func=albumentations.pytorch.ToTensorV2(),
        preprocess_func=torch.from_numpy,
        **model_info.dataset_args
    )

    model.train()
    optimizer = radam.RAdam(model.parameters(), lr=model_info.initial_lr)

    milestones = [4, 8, 12]
    scheduler = optim.lr_scheduler.MultiStepLR(optimizer, milestones=milestones, gamma=0.2)

    print(f'Num training images: {len(dataset_train)} validation images: {len(dataset_valid)}')

    if resume_epoch > -1:
        checkpoint = torch.load(f'{checkpoints_dir}/{resume_epoch:03}.pt')
        model.module.load_state_dict(checkpoint['model_state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer_state_dict'])

    data_loaders = {
        'train': DataLoader(dataset_train,
                            num_workers=16,
                            batch_size=model_info.batch_size),
        'val':   DataLoader(dataset_valid,
                            shuffle=False,
                            num_workers=16,
                            batch_size=8)
    }

    criterium = nn.BCEWithLogitsLoss()

    # fit new layers first:
    if resume_epoch == -1 and model_info.is_pretrained:
        model.train()
        model.module.freeze_encoder()
        data_loader = data_loaders['train']
        pre_fit_steps = 20000 // model_info.batch_size
        data_iter = tqdm(enumerate(data_loader), total=pre_fit_steps)
        epoch_loss = []
        initial_optimizer = radam.RAdam(model.parameters(), lr=1e-3)
        for iter_num, data in data_iter:
            if iter_num > pre_fit_steps:
                break
            with torch.set_grad_enabled(True):
                img = data['image'].float().cuda()
                labels = data['labels'].cuda()
                pred = model(img)
                loss = criterium(pred, labels)
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 10.0)
                initial_optimizer.step()
                initial_optimizer.zero_grad()
                epoch_loss.append(float(loss))

                data_iter.set_description(f'Loss: Running {np.mean(epoch_loss[-500:]):1.4f} Avg {np.mean(epoch_loss):1.4f}')
    model.module.unfreeze_encoder()

    for epoch_num in range(resume_epoch+1, 128):
        for phase in ['train', 'val']:
            model.train(phase == 'train')
            epoch_loss = []

            data_loader = data_loaders[phase]
            data_iter = tqdm(enumerate(data_loader), total=len(data_loader))
            for iter_num, data in data_iter:
                img = data['image'].float().cuda()
                labels = data['labels'].cuda()

                with torch.set_grad_enabled(phase == 'train'):
                    pred = model(img)
                    loss = criterium(pred, labels)

                    if phase == 'train':
                        (loss / model_info.accumulation_steps).backward()
                        if (iter_num + 1) % model_info.accumulation_steps == 0:
                            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                            optimizer.step()
                            optimizer.zero_grad()

                    epoch_loss.append(float(loss))

                data_iter.set_description(
                    f'{epoch_num} Loss: Running {np.mean(epoch_loss[-1000:]):1.4f} Avg {np.mean(epoch_loss):1.4f}')

            logger.add_scalar(f'loss_{phase}', np.mean(epoch_loss), epoch_num)
            logger.add_scalar('lr', optimizer.param_groups[0]['lr'], epoch_num)  # scheduler.get_lr()[0]
            logger.flush()

            if phase == 'val':
                scheduler.step(epoch=epoch_num)

                print(f'{checkpoints_dir}/{epoch_num:03}.pt')
                torch.save(
                    {
                        'epoch': epoch_num,
                        'model_state_dict': model.module.state_dict(),
                        'optimizer_state_dict': optimizer.state_dict(),
                    },
                    f'{checkpoints_dir}/{epoch_num:03}.pt'
                )


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', type=str, default='check')
    parser.add_argument('--model', type=str, default='')
    parser.add_argument('--run', type=str, default='')
    parser.add_argument('--fold', type=int, default=-1)
    parser.add_argument('--weights', type=str, default='')
    parser.add_argument('--epoch', type=int, default=-1)

    parser.add_argument('--resume_weights', type=str, default='')
    parser.add_argument('--resume_epoch', type=int, default=-1)

    args = parser.parse_args()
    action = args.action

    if action == 'train':
        try:
            train(model_name=args.model, run=args.run, fold=args.fold, resume_epoch=args.resume_epoch)
        except KeyboardInterrupt:
            pass
