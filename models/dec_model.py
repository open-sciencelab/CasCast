import torch
from models.model import basemodel
import torch.cuda.amp as amp
from torch.functional import F
from torch.distributions import Normal
import time
import copy
from megatron_utils import mpu
import numpy as np

import utils.misc as utils

import wandb

### paralle ensemble ###
from megatron_utils.parallel_state import get_ensemble_parallel_group

class dec_model(basemodel):
    def __init__(self, logger, **params) -> None:
        super().__init__(logger, **params)
        self.logger_print_time = False
        self.data_begin_time = time.time()
        self.rss_training = params.get('rss_training', False)

    def data_preprocess(self, data):
        data_dict = {}
        inp_data = data['inputs'].float().to(self.device, non_blocking=True, dtype=self.data_type)
        tar_data = data['data_samples'].float().to(self.device, non_blocking=True, dtype=self.data_type)

        inp_length = inp_data.shape[1]
        pred_length = tar_data.shape[1]
        total_length = inp_length + pred_length
        data_dict.update({'input_length':inp_length, 'total_length':total_length})
        ## get mask for scheduled training schedule ##
        rss_training = 0 if not self.rss_training else 1
        data_dict.update({'rss': rss_training})
        data_dict.update({'mask_true': torch.zeros_like(torch.cat([inp_data, tar_data], dim=1))})
        ## get frame tensors concat(inp, output) ##
        ## 1->T are targets ##
        data_dict.update({'inputs': torch.cat([inp_data, tar_data], dim=1)})
        data_dict.update({'data_samples': torch.cat([inp_data, tar_data], dim=1)[:,1:]})

        return data_dict


    def train_one_step(self, batch_data, step):
        data_dict = self.data_preprocess(batch_data)
        inp, tar = data_dict['inputs'], data_dict['data_samples']
        input_length, total_length = data_dict['input_length'], data_dict['total_length']
        rss, mask_true = data_dict['rss'], data_dict['mask_true']
        self.optimizer[list(self.model.keys())[0]].zero_grad()
        with amp.autocast(enabled=self.enabled_amp):
            prediction = self.model[list(self.model.keys())[0]](frames_tensor=inp, mask_true=mask_true, input_length=input_length, total_length=total_length, reverse_scheduled_sampling=rss)
            loss = self.loss(prediction, tar)
            self.gscaler.scale(loss).backward()
            
        self.gscaler.step(self.optimizer[list(self.model.keys())[0]])
        self.gscaler.update()
        # if (utils.get_world_size() > 1 and mpu.get_data_parallel_rank() == 0) or utils.get_world_size() == 1:
        #             wandb.log({f'train_{self.loss_type}': loss.item() })
        
        if self.visualizer_type is None:
            pass
        elif self.visualizer_type == 'hko7_visualizer' and (step) % self.visualizer_step==0:
            self.visulizer.save_dbz_image(pred_image=prediction[:, input_length-1:], target_img=tar[:, input_length-1:], step=step)
        elif self.visualizer_type == 'sevir_visualizer' and (step) % self.visualizer_step==0:
            self.visualizer.save_pixel_image(pred_image=prediction[:, input_length-1:], target_img=tar[:, input_length-1:], step=step)
        elif self.visualizer_type == 'meteonet_visualizer' and (step) % self.visualizer_step == 0:
            self.visualizer.save_pixel_image(pred_image=prediction[:, input_length-1:], target_img=tar[:, input_length-1:], step=step)
        else:
            pass
        return {self.loss_type: loss.item()}
    


    def test_one_step(self, batch_data):
        data_dict = self.data_preprocess(batch_data)
        inp, tar = data_dict['inputs'], data_dict['data_samples']
        input_length, total_length = data_dict['input_length'], data_dict['total_length']
        rss, mask_true = data_dict['rss'], data_dict['mask_true']
        prediction = self.model[list(self.model.keys())[0]](frames_tensor=inp, mask_true=mask_true, input_length=input_length, total_length=total_length, reverse_scheduled_sampling=0)
        loss_records = {}
        ## evaluate other metrics ##
        data_dict = {}
        data_dict['gt'] = tar[:, input_length-1:]
        data_dict['pred'] = prediction[:, input_length-1:]
        MSE_loss = torch.mean((prediction[:, input_length-1:] - tar[:, input_length-1:]) ** 2).item()
        loss = self.loss(prediction, tar)
        
        if self.metrics_type == 'hko7_official':
            import pdb; pdb.set_trace()
            data_dict['gt'] = data_dict['gt'].squeeze(2).cpu().numpy()
            data_dict['pred'] = data_dict['pred'].squeeze(2).cpu().numpy()
            self.eval_metrics.update(gt=data_dict['gt'], pred=data_dict['pred'], mask=self.eval_metrics._exclude_mask)
            csi, mse, mae = self.eval_metrics.calculate_stat()
            for i, thr in enumerate(self.eval_metrics._thresholds):
                loss_records.update({f'CSI_{thr}': csi[:, i].mean()})
            loss_records.update({'MSE': MSE_loss})
        elif self.metrics_type == 'SEVIRSkillScore':
            import pdb; pdb.set_trace() ##TODO: check the metrics
            csi_total = 0
            ## to pixel ##
            data_dict['gt'] = data_dict['gt'].squeeze(2) * 255
            data_dict['pred'] = data_dict['pred'].squeeze(2) * 255
            self.eval_metrics.update(target=data_dict['gt'].cpu(), pred=data_dict['pred'].cpu())
            metrics = self.eval_metrics.compute()
            for i, thr in enumerate(self.eval_metrics.threshold_list):
                loss_records.update({f'CSI_{thr}': metrics[thr
                ]['csi']})
                csi_total += metrics[thr]['csi']
            loss_records.update({'CSI_m': csi_total / len(self.eval_metrics.threshold_list)})
            loss_records.update({'MSE': MSE_loss})
            if (utils.get_world_size() > 1 and mpu.get_data_parallel_rank() == 0) or utils.get_world_size() == 1:
                wandb.log({f'val_CSI_m': loss_records['CSI_m'] })
        elif self.metrics_type == 'METEONETScore':
            data_dict['gt'] = data_dict['gt']
            data_dict['pred'] = data_dict['pred']
            self.eval_metrics.update(target=data_dict['gt'], pred=data_dict['pred'])
            loss_records.update({'MSE': MSE_loss})
        else:
            metrics_loss = self.eval_metrics.evaluate_batch(data_dict)
            loss_records.update(metrics_loss)
        
        # ## log to wandb ##
        # if (utils.get_world_size() > 1 and mpu.get_data_parallel_rank() == 0) or utils.get_world_size() == 1:
        #     wandb.log({f'val_{self.loss_type}': loss.item() })
        return loss_records
    
    
    @torch.no_grad()
    def test(self, test_data_loader, epoch):

        metric_logger = utils.MetricLogger(delimiter="  ", sync=True)
        # set model to eval
        for key in self.model:
            self.model[key].eval()
        data_loader = test_data_loader

        ## save some results ##
        for step, batch in enumerate(data_loader):
            if self.debug and step>= 2 and self.sub_model_name[0] != "IDLE":
                break
            # if self.debug and step>= 2:
            #     break
            if isinstance(batch, int):
                batch = None

            loss = self.test_one_step(batch)
            metric_logger.update(**loss)

        ## compute metrics ##
        losses = {}
        metrics = self.eval_metrics.compute()
        for thr, thr_dict in metrics.items():
            for k, v in thr_dict.items():
                losses.update({f'{thr}-{k}': v})
        self.eval_metrics.reset()
        metric_logger.update(**losses)
        self.logger.info('  '.join(
                [f'Epoch [{epoch + 1}](val stats)',
                 "{meters}"]).format(
                    meters=str(metric_logger)
                 ))

        return metric_logger
    

    # def eval_step(self, batch_data):
    #     data_dict = self.data_preprocess(batch_data)
    #     inp, tar = data_dict['inputs'], data_dict['data_samples']
    #     input_length, total_length = data_dict['input_length'], data_dict['total_length']
    #     rss, mask_true = data_dict['rss'], data_dict['mask_true']
    #     prediction = self.model[list(self.model.keys())[0]](frames_tensor=inp, mask_true=mask_true, input_length=input_length, total_length=total_length, reverse_scheduled_sampling=0)

    #     ### the official hko7 evaluator receive input tensor shape: b, t, h, w ##
    #     losses = {}
    #     data_dict = {}
    #     if self.metrics_type == 'hko7_official':
    #         data_dict.update({'gt': tar[:, input_length-1:].squeeze(2).cpu().numpy()})
    #         data_dict.update({'pred': prediction[:, input_length-1:].squeeze(2).cpu().numpy()})
    #         self.eval_metrics.update(gt=data_dict['gt'], pred=data_dict['pred'], mask=self.eval_metrics._exclude_mask)
    #         csi, mse, mae = self.eval_metrics.calculate_stat()
    #         for i, thr in enumerate(self.eval_metrics._thresholds):
    #             losses.update({f'CSI_{thr}': csi[:, i].mean()})
    #     elif self.metrics_type == 'SEVIRSkillScore':
    #         ## to pixel ##
    #         data_dict['gt'] = tar[:, input_length-1:].squeeze(2) * 255
    #         data_dict['pred'] = prediction[:, input_length-1:].squeeze(2) * 255
    #         self.eval_metrics.update(target=data_dict['gt'].cpu(), pred=data_dict['pred'].cpu())
    #         metrics = self.eval_metrics.compute()
    #         csi_total = 0
    #         for i, thr in enumerate(self.eval_metrics.threshold_list):
    #             losses.update({f'CSI_{thr}': metrics[thr
    #             ]['csi']})
    #             csi_total += metrics[thr]['csi']
    #         losses.update({'CSI_m': csi_total / len(self.eval_metrics.threshold_list)})
    #         losses.update({'pred_MSE': torch.mean((prediction[:, input_length-1:] - tar[:, input_length-1:]) ** 2).item()})

    #     return losses
    
    @torch.no_grad()
    def eval_step(self, batch_data, step):
        data_dict = self.data_preprocess(batch_data)
        inp, tar = data_dict['inputs'], data_dict['data_samples']
        input_length, total_length = data_dict['input_length'], data_dict['total_length']
        rss, mask_true = data_dict['rss'], data_dict['mask_true']
        prediction = self.model[list(self.model.keys())[0]](frames_tensor=inp, mask_true=mask_true, input_length=input_length, total_length=total_length, reverse_scheduled_sampling=0)
        loss_records = {}

        data_dict = {}
        data_dict['gt'] = tar[:, input_length-1:]
        data_dict['pred'] = prediction[:, input_length-1:]

        ### the official hko7 evaluator receive input tensor shape: b, t, h, w ##
        losses = {}
        if self.metrics_type == 'hko7_official':
            # import pdb; pdb.set_trace() ##TODO: check metrics
            # data_dict.update({'gt': tar.squeeze(2).cpu().numpy()})
            # data_dict.update({'pred': prediction.squeeze(2).cpu().numpy()})
            # self.eval_metrics.update(gt=data_dict['gt'], pred=data_dict['pred'], mask=self.eval_metrics._exclude_mask)
            # csi, mse, mae = self.eval_metrics.calculate_stat()
            # for i, thr in enumerate(self.eval_metrics._thresholds):
            #     losses.update({f'CSI_{thr}': csi[:, i].mean()})
            self.eval_metrics.update(target=data_dict['gt'], pred=data_dict['pred'])
            ############
            sf_dict = self.eval_metrics.get_single_frame_metrics(target=data_dict['gt'], pred=data_dict['pred'])
            crps_dict = self.eval_metrics.get_crps(target=data_dict['gt'], pred=data_dict['pred'])
            losses.update(sf_dict)
            losses.update(crps_dict)
            ############
        elif self.metrics_type == 'SEVIRSkillScore':
            self.eval_metrics.update(target=data_dict['gt'], pred=data_dict['pred'])
            ############
            sf_dict = self.eval_metrics.get_single_frame_metrics(target=data_dict['gt'], pred=data_dict['pred'])
            crps_dict = self.eval_metrics.get_crps(target=data_dict['gt'], pred=data_dict['pred'])
            losses.update(sf_dict)
            losses.update(crps_dict)
            ############
            self.fvd_computer.update(videos_real=data_dict['gt'].repeat(1, 1, 3, 1, 1), videos_fake=data_dict['pred'].repeat(1, 1, 3, 1, 1))
            ############
        elif self.metrics_type == 'METEONETScore':
            self.eval_metrics.update(target=data_dict['gt'], pred=data_dict['pred'])
            ############
            sf_dict = self.eval_metrics.get_single_frame_metrics(target=data_dict['gt'], pred=data_dict['pred'])
            crps_dict = self.eval_metrics.get_crps(target=data_dict['gt'], pred=data_dict['pred'])
            losses.update(sf_dict)
            losses.update(crps_dict)
        losses.update({'MSE': torch.mean((prediction - tar) ** 2).item()})

        ## save image ##
        if self.visualizer_type == 'sevir_visualizer' and (step) % 1 == 0:
            # self.visualizer.save_pixel_image(pred_image=data_dict['pred'], target_img=data_dict['gt'], step=step)
            #######################################################################################################
            model_name = list(self.model.keys())[0]
            ceph_prefix = f'radar:s3://radar_visualization/sevir/{model_name}_{self.visualizer.sub_dir}'
            self.visualizer.save_vil_last_image_and_npy(pred_image=data_dict['pred'], target_img=data_dict['gt'], step=step, ceph_prefix=ceph_prefix)
        elif self.visualizer_type == 'hko7_visualizer' and (step) % 1 == 0:
            # self.visualizer.save_pixel_image(pred_image=data_dict['pred'], target_img=data_dict['gt'], step=step)
            #######################################################################################################
            model_name = list(self.model.keys())[0]
            ceph_prefix = f'radar:s3://radar_visualization/hko7/{model_name}_{self.visualizer.sub_dir}'
            self.visualizer.save_hko7_last_image_and_npy(pred_image=data_dict['pred'], target_img=data_dict['gt'], step=step, ceph_prefix=ceph_prefix)
        elif self.visualizer_type == 'meteonet_visualizer' and (step) % 1 == 0:
            # self.visualizer.save_pixel_image(pred_image=data_dict['pred'], target_img=data_dict['gt'], step=step)
            model_name = list(self.model.keys())[0]
            ceph_prefix = f'radar:s3://radar_visualization/meteonet/{model_name}_{self.visualizer.sub_dir}'
            self.visualizer.save_meteo_last_image_and_npy(pred_image=data_dict['pred'], target_img=data_dict['gt'], step=step, ceph_prefix=ceph_prefix)
        else:
            pass
        return losses
    
    @torch.no_grad()
    def test_final(self, test_data_loader, predict_length):
        self.test_data_loader = test_data_loader
        metric_logger = utils.MetricLogger(delimiter="  ", sync=True)
        # set model to eval
        for key in self.model:
            self.model[key].eval()

        if utils.get_world_size() > 1:
            rank = mpu.get_data_parallel_rank()
            world_size = mpu.get_data_parallel_world_size()
        else:
            rank = 0
            world_size = 1

        if test_data_loader is not None:
            data_loader = test_data_loader
        else:
            raise ValueError("test_data_loader is None")
        from utils.metrics import cal_FVD
        self.fvd_computer = cal_FVD(use_gpu=True)

        from megatron_utils.tensor_parallel.data import get_data_loader_length
        total_step = get_data_loader_length(test_data_loader)
        ## save some results ##
        self.num_results2save = 5
        self.id_results2save = 0
        for step, batch in enumerate(data_loader):
            if isinstance(batch, int):
                batch = None
            losses = self.eval_step(batch_data=batch, step=step)
            metric_logger.update(**losses)

            self.logger.info("#"*80)
            self.logger.info(step)
            if step % 10 == 0 or step == total_step-1:
                self.logger.info('  '.join(
                [f'Step [{step + 1}](val stats)',
                 "{meters}"]).format(
                    meters=str(metric_logger)
                 ))
        # ####################################################
        # fvd = self.fvd_computer.compute()
        # losses.update({'fvd':fvd})
        # ####################################################
        metrics = self.eval_metrics.compute()
        for thr, thr_dict in metrics.items():
            for k, v in thr_dict.items():
                losses.update({f'{thr}-{k}': v})
        # ###################################################
        # metric_logger.update(**losses)
        # self.logger.info('final results: {meters}'.format(meters=str(metric_logger)))
        ##################################################
        ## save as excel ##
        import pandas as pd
        df = pd.DataFrame.from_dict(losses)
        df.to_excel(f'{self.visualizer.exp_dir}/{self.visualizer.sub_dir}_losses.xlsx')
        return None