import pytorch_lightning as pl
import torch
import torchvision
import torch.nn as nn
import torch.nn.functional as F
from typing import Optional, Union, List
from utils.model_utils import get_backbone_from_name
from utils.data_utils import  get_metric_func
#from miners.triplet_margin_miner import TripletMarginMiner
from pytorch_metric_learning import losses, miners, distances, reducers
import torchmetrics
from sklearn.metrics import f1_score
import numpy as np
from torch.nn import TransformerEncoderLayer
from typing import Optional, Any
from torch import Tensor
import math
import geotorch

class AUClassifier(nn.Module):
    def __init__(self, in_channels, out_channels):
        super().__init__()
        self.fc = nn.Linear(in_channels, out_channels)

    def forward(self, seq_input):
        bs, seq_len = seq_input.size(0), seq_input.size(1)
        weight = self.fc.weight
        bias = self.fc.bias
        seq_input = seq_input.reshape((bs*seq_len, 1, -1)) # bs*seq_len, 1, metric_dim
        weight = weight.unsqueeze(0).repeat((bs, 1, 1))  # bs,seq_len, metric_dim
        weight = weight.view((bs*seq_len, -1)).unsqueeze(-1) #bs*seq_len, metric_dim, 1
        inner_product = torch.bmm(seq_input, weight).squeeze(-1).squeeze(-1) # bs*seq_len
        inner_product = inner_product.view((bs, seq_len))
        return inner_product + bias

class Model(pl.LightningModule):
    def __init__(self, *args, **kwargs):
        super(Model, self).__init__(*args, **kwargs)

    def forward(self, x):
        raise NotImplementedError
    def training_step(self, batch, batch_idx):
        raise NotImplementedError

    def configure_optimizers(self,):
        raise NotImplementedError
    def validation_step(self, batch, batch_idx, dataloader_idx):
        raise NotImplementedError
    def validation_epoch_end(self, validation_step_outputs):
        raise NotImplementedError

    def configure_optimizers(self):
        paramters_dict = [{'params':self.parameters() , 'lr':self.lr}]
        optimizer = torch.optim.SGD(paramters_dict, momentum=0.9,
                lr=self.lr,
                weight_decay = self.wd)
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
                optimizer, T_max=self.T_max)

        return {
        'optimizer': optimizer,
        'lr_scheduler': {'scheduler': scheduler, 'interval': 'step'}
        } # cosine annealing scheduler

class MultitaskModel(Model):
    def __init__(self, *args, **kwargs):
        super(MultitaskModel, self).__init__(*args, **kwargs)

    def validation_step(self, batch, batch_idx, dataloader_idx):
        # the batch input: input_image, label
        x, y = batch 
        preds,_  = self(x) # the batch output: preds_dictionary, metrics
        return torch.cat([preds['AU'], preds['EXPR'], preds['VA']], dim=-1), y
    def parse_multiple_labels(self, y, AU=False, EXPR=False, VA=False):
        if AU and VA and (not EXPR):
            y_au = y[:, :len(self.au_names_list)].float()
            y_va = y[:, len(self.au_names_list):].float()
            return y_au, y_va
        if AU and VA and EXPR:
            y_au = y[:, :len(self.au_names_list)].float()
            y_expr = y[:, len(self.au_names_list)].long()
            y_va = y[:, len(self.au_names_list)+1:].float()
            return y_au,y_expr, y_va
class InceptionV3MTModel(MultitaskModel):
	# the intialization function is shared by all models with InceptionV3 feature extractor
    def __init__(self, backbone:str, tasks: List[str], 
        au_names_list: List[str], emotion_names_list: List[str], va_dim:int,
        AU_metric_dim: int, 
        n_heads:int = 8,
        dropout = 0.3, 
        lr:float=1e-3, 
        T_max:int = 1e4, wd:float=0.,
        AU_cls_loss_func = None,
        EXPR_cls_loss_func = None,
        VA_cls_loss_func = None,
        AU_metric_loss_func = None,
        EXPR_metric_loss_func = None,
        VA_metric_loss_func = None,
        avg_features = True): 
        super(InceptionV3MTModel, self).__init__() 
        self.tasks = tasks
        self.backbone_CNN = get_backbone_from_name(backbone, pretrained=True, remove_classifier=True)
        self.au_names_list = au_names_list
        self.emotion_names_list = emotion_names_list
        self.va_dim = va_dim
        self.AU_metric_dim = AU_metric_dim
        self.n_heads = n_heads
        self.dropout = dropout
        self.lr = lr
        self.T_max = T_max
        self.wd = wd
        self.AU_cls_loss_func = AU_cls_loss_func
        self.EXPR_cls_loss_func = EXPR_cls_loss_func
        self.VA_cls_loss_func = VA_cls_loss_func

        self.N_emotions = len(self.emotion_names_list)
        self.AU_metric_loss_func = AU_metric_loss_func
        self.EXPR_metric_loss_func = EXPR_metric_loss_func
        self.VA_metric_loss_func = VA_metric_loss_func
        self.features_dim = self.backbone_CNN.features_dim
        self.features_width = self.backbone_CNN.features_width
        self.avg_features = avg_features

        self.configure_architecture() # define unique model architecture

    def training_task(self, preds_task, labels_task, metrics_task, task):
        cls_loss_func = getattr(self, task+'_cls_loss_func')
        cls_loss_values =  cls_loss_func(preds_task, labels_task)
        metric_loss_func = getattr(self, task+'_metric_loss_func')
        if metric_loss_func is not None:
            metric_loss_value = metric_loss_func(metrics_task, labels_task)
        else:
             metric_loss_value = 0
        return cls_loss_values+metric_loss_value
    def training_step(self, batch, batch_idx):
        # import pdb; pdb.set_trace()
        (x_au, y_au), (x_expr, y_expr), (x_va, y_va) = batch['single'] 
        if 'multiple' in batch.keys():
            (x_au_va, y_au_va), (x_au_expr_va, y_au_expr_va) = batch['multiple']
            preds_au_va, metrics_au_va  = self(x_au_va)
            preds_au_expr_va, metrics_au_expr_va = self(x_au_expr_va)
        total_loss = 0 

        for task in self.tasks:
            if task =='AU':
                preds, metrics  = self(x_au)
                preds_task = preds[task] if 'multiple' not in batch.keys() else torch.cat([preds[task], preds_au_va[task]], dim=0)
                labels_task = y_au if 'multiple' not in batch.keys() else torch.cat([y_au, self.parse_multiple_labels(y_au_va, AU=True, VA=True)[0]], dim=0)
                metrics_task = metrics[task] if 'multiple' not in batch.keys() else torch.cat([metrics[task], metrics_au_va[task]], dim=0)
            elif task =='EXPR':
                preds, metrics  = self(x_expr)
                preds_task = preds[task] if 'multiple' not in batch.keys() else torch.cat([preds[task], preds_au_expr_va[task]], dim=0)
                labels_task = y_expr if 'multiple' not in batch.keys() else torch.cat([y_expr, self.parse_multiple_labels(y_au_expr_va, AU=True, EXPR=True, VA=True)[1]], dim=0)
                metrics_task = metrics[task] if 'multiple' not in batch.keys() else torch.cat([metrics[task], metrics_au_expr_va[task]], dim=0)
            elif task =='VA':
                preds, metrics  = self(x_va)
                preds_task = preds[task] if 'multiple' not in batch.keys() else torch.cat([preds[task], preds_au_va[task], preds_au_expr_va[task]], dim=0)
                labels_task = y_va if 'multiple' not in batch.keys() else torch.cat([y_va, 
                self.parse_multiple_labels(y_au_va, AU=True, VA=True)[-1] ,
                self.parse_multiple_labels(y_au_expr_va, AU=True, EXPR=True, VA=True)[-1]], dim=0)
                metrics_task = metrics[task] if 'multiple' not in batch.keys() else torch.cat([metrics[task], metrics_au_va[task], metrics_au_expr_va[task]], dim=0)
            loss = self.training_task(preds_task, labels_task, metrics_task, task)
            self.log('loss_{}'.format(task), loss, on_step=True, on_epoch=True, 
                prog_bar=True, logger=True)
            total_loss += loss
        self.log('total_loss'.format(task), total_loss, on_step=True, on_epoch=True, 
            prog_bar=True, logger=True)
        return total_loss


    def save_val_metrics_task(self, preds_task, labels_task, save_name, task):
        metric_values, _ = get_metric_func(task)(preds_task.numpy(), labels_task.numpy())
        if task != 'VA':
            self.log('{}_F1'.format(save_name), metric_values[0], on_epoch=True, logger=True)
            self.log('{}_Acc'.format(save_name), metric_values[1], on_epoch=True, logger=True)
            return metric_values[0] # return F1 for EXPR and AU
        else:
            self.log('{}_{}'.format(save_name, 'valence'), metric_values[0], on_epoch=True, logger=True)  
            self.log('{}_{}'.format(save_name, 'arousal'), metric_values[1], on_epoch=True, logger=True) 
            return 0.5*metric_values[0]+0.5*metric_values[1]
    def turn_mul_emotion_preds_to_dict(self, preds):
        # au, expr , va
        return {'AU': preds[..., :len(self.au_names_list)], 
        'EXPR': preds[..., len(self.au_names_list): len(self.au_names_list)+len(self.emotion_names_list)],
        'VA': preds[..., -2:]}

    def validation_epoch_end(self, validation_step_outputs):
        #check the validation step outputs
        # import pdb; pdb.set_trace()
        num_dataloaders = len(validation_step_outputs) # three or five
        total_metric = 0
        for dataloader_idx in range(num_dataloaders):
            val_dl_outputs = validation_step_outputs[dataloader_idx]
            idx_metric = self.validation_on_single_dataloader(dataloader_idx, val_dl_outputs)
            total_metric+=idx_metric
        self.log('val_total', total_metric, on_epoch=True, logger=True) 
    
    def validation_on_single_dataloader(self, dataloader_idx, val_dl_outputs):
        num_batches = len(val_dl_outputs)
        preds = torch.cat([x[0] for x in val_dl_outputs], dim=0).cpu()
        labels= torch.cat([x[1] for x in val_dl_outputs], dim=0).cpu()
        preds = self.turn_mul_emotion_preds_to_dict(preds)
        if len(labels.size())>1 and labels.size(-1) == len(self.au_names_list)+2:
            # au_va
            metrics_aus = self.validation_single_task(dataloader_idx, preds,
             labels[:, :len(self.au_names_list)])
            metrics_va = self.validation_single_task(dataloader_idx, preds,
             labels[:, len(self.au_names_list):])
            idx_metric =  (metrics_aus+metrics_va)*0.5

        elif len(labels.size())>1 and labels.size(-1) == len(self.au_names_list)+1+2:
            # au_expr_va
            metrics_aus = self.validation_single_task(dataloader_idx, preds,
             labels[:, : len(self.au_names_list)])
            metrics_expr = self.validation_single_task(dataloader_idx, preds,
             labels[:, len(self.au_names_list)])
            metrics_va = self.validation_single_task(dataloader_idx, preds,
             labels[:, -2:])
            idx_metric = (metrics_aus+metrics_expr+metrics_va)*(1/3)
        else:
            idx_metric = self.validation_single_task(dataloader_idx, preds, labels)
        
        return idx_metric
    def validation_single_task(self, dataloader_idx, preds, labels):
        if len(labels.size())==1:
            # expr
            preds_task = F.softmax(preds['EXPR'], dim=-1).argmax(-1).int()
            labels_task = labels.int()
            metric_8_task = self.save_val_metrics_task(preds_task, 
                labels_task,  "D{}/EXPR8".format(dataloader_idx), 'EXPR')
            mask = labels_task<7
            if sum(mask)>0:
                metric_7_task = self.save_val_metrics_task(preds_task[mask], 
                    labels_task[mask],  "D{}/EXPR7".format(dataloader_idx), 'EXPR')
            return metric_8_task

        elif labels.size(1)==12:
            # au
            preds_task = (torch.sigmoid(preds['AU'])>0.5).int()
            labels_task = labels.int()
            metric_aus = self.save_val_metrics_task(preds_task, 
                labels_task,  "D{}/AU".format(dataloader_idx), 'AU')
            return metric_aus
        elif labels.size(1) == 2:
            # va
            preds_task = preds['VA'].float()
            labels_task = labels
            metric_va = self.save_val_metrics_task(preds_task, 
                labels_task,  "D{}/VA".format(dataloader_idx), 'VA')
            return metric_va