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
from tqdm.auto import tqdm
import torch.distributed as dist

import wandb

from einops import rearrange

class latent_diffusion_direct_pred_128_model(basemodel):
    def __init__(self, logger, **params) -> None:
        super().__init__(logger, **params)
        self.logger_print_time = False
        self.data_begin_time = time.time()

        self.diffusion_kwargs = params.get('diffusion_kwargs', {})

        ## init noise scheduler ##
        self.noise_scheduler_kwargs = self.diffusion_kwargs.get('noise_scheduler', {})
        self.noise_scheduler_type = list(self.noise_scheduler_kwargs.keys())[0]
        if self.noise_scheduler_type == 'DDPMScheduler':
            from src.diffusers import DDPMScheduler
            self.noise_scheduler = DDPMScheduler(**self.noise_scheduler_kwargs[self.noise_scheduler_type])
            num_train_timesteps = self.noise_scheduler_kwargs[self.noise_scheduler_type]['num_train_timesteps']
            self.noise_scheduler.set_timesteps(num_train_timesteps)
        elif self.noise_scheduler_type == 'DPMSolverMultistepScheduler':
            from src.diffusers import DPMSolverMultistepScheduler
            import pdb; pdb.set_trace()
            self.noise_scheduler = DPMSolverMultistepScheduler(**self.noise_scheduler_kwargs[self.noise_scheduler_type])
            num_train_timesteps = self.noise_scheduler_kwargs[self.noise_scheduler_type]['num_train_timesteps']
            self.noise_scheduler.set_timesteps(num_train_timesteps)
        else:
            raise NotImplementedError
        
        ## init noise scheduler for sampling ##
        self.sample_noise_scheduler_type = 'DDIMScheduler'
        if self.sample_noise_scheduler_type == 'DDIMScheduler':
            print("############# USING SAMPLER: DDIMScheduler #############")
            from src.diffusers import DDIMScheduler
            self.sample_noise_scheduler = DDIMScheduler(**self.noise_scheduler_kwargs[self.noise_scheduler_type])
            ## set num of inference
            self.sample_noise_scheduler.set_timesteps(20)
        elif self.sample_noise_scheduler_type == 'DDPMScheduler':
            print("############# USING SAMPLER: DDPMScheduler #############")
            from src.diffusers import DDPMScheduler
            self.sample_noise_scheduler = DDPMScheduler(**self.noise_scheduler_kwargs[self.noise_scheduler_type])
            self.sample_noise_scheduler.set_timesteps(1000)
        else:
            raise NotImplementedError

        ## important: scale the noise to get a reasonable noise process ##
        self.noise_scale = self.noise_scheduler_kwargs.get('noise_scale', 1.0)
        self.logger.info(f'####### noise scale: {self.noise_scale} ##########')

        ## load pretrained checkpoint ##
        self.predictor_ckpt_path = self.extra_params.get("predictor_checkpoint_path", None)
        print(f'load from predictor_ckpt_path: {self.predictor_ckpt_path}')
        self.load_checkpoint(self.predictor_ckpt_path, load_model=True, load_optimizer=False, load_scheduler=False, load_epoch=False, load_metric_best=False)
        
        self.autoencoder_ckpt_path = self.extra_params.get("autoencoder_checkpoint_path", None)
        print(f'load from autoencoder_ckpt_path: {self.autoencoder_ckpt_path}')
        self.load_checkpoint(self.autoencoder_ckpt_path, load_model=True, load_optimizer=False, load_scheduler=False, load_epoch=False, load_metric_best=False)

        ## scale factor ##
        self.scale_factor = 1.0 ## 1/std TODO: according to latent space
        self.logger.info(f'####### USE SCALE_FACTOR: {self.scale_factor} ##########')

        ## classifier free guidance ##
        self.classifier_free_guidance_kwargs = self.diffusion_kwargs.get('classifier_free_guidance', {})
        self.p_uncond = self.classifier_free_guidance_kwargs.get('p_uncond', 0.0)
        self.guidance_weight = self.classifier_free_guidance_kwargs.get('guidance_weight', 0.0)
        


    def data_preprocess(self, data):
        data_dict = {}
        original_tar = data['data_samples'].float().to(self.device, non_blocking=True, dtype=self.data_type)
        original_inp = data['inputs'].float().to(self.device, non_blocking=True, dtype=self.data_type)

        inp_data = F.interpolate(original_inp.squeeze(2), (128, 128)).unsqueeze(2)
        tar_data = F.interpolate(original_tar.squeeze(2), (128, 128)).unsqueeze(2)
        data_dict.update({'inputs': inp_data, 'data_samples': tar_data, 'original_tar': original_tar})
        return data_dict
    
    @torch.no_grad()
    def denoise(self, template_data, cond_data, bs=1, vis=False, cfg=1, ensemble_member=1):
        """
        denoise from gaussian.
        """
        _, t, c, h, w = template_data.shape
        cond_data = cond_data[:bs, ...]
        generator = torch.Generator(device=template_data.device) #torch.manual_seed(0)
        generator.manual_seed(0)
        latents = torch.randn(
            (bs*ensemble_member, t, c, h, w),
            generator=generator,
            device=template_data.device,
        ) 
        latents = latents * self.sample_noise_scheduler.init_noise_sigma

        print("start sampling")
        if cfg == 1:
            assert ensemble_member == 1
            ## iteratively denoise ##
            for t in tqdm(self.sample_noise_scheduler.timesteps) if (self.debug or vis) else self.sample_noise_scheduler.timesteps:
                ## predict the noise residual ##
                timestep = torch.ones((bs,), device=template_data.device) * t
                noise_pred = self.model[list(self.model.keys())[0]](x=latents, timesteps=timestep, cond=cond_data)
                ## compute the previous noisy sample x_t -> x_{t-1} ##
                latents = self.sample_noise_scheduler.step(noise_pred, t, latents).prev_sample
            print("end sampling")
            return latents
        else:
            print(f"guidance strength: {cfg}")
            ## for classifier free sampling ##
            cond_data = torch.cat([cond_data, torch.zeros_like(cond_data)])
            # prev_x = torch.cat([prev_x, prev_x])
            avg_latents = []
            for member in range(ensemble_member):
                member_latents = latents[member*bs:(member+1)*bs, ...]
                for t in tqdm(self.sample_noise_scheduler.timesteps) if (self.debug or vis) else self.sample_noise_scheduler.timesteps:
                    ## predict the noise residual ##
                    timestep = torch.ones((bs*2,), device=template_data.device) * t
                    latent_model_input = torch.cat([member_latents]*2)
                    latent_model_input = self.sample_noise_scheduler.scale_model_input(latent_model_input, t)
                    noise_pred = self.model[list(self.model.keys())[0]](x=latent_model_input, timesteps=timestep, cond=cond_data)
                    ########################
                    noise_pred_cond, noise_pred_uncond = noise_pred.chunk(2)
                    noise_pred = noise_pred_uncond + cfg*(noise_pred_cond - noise_pred_uncond)
                    ## compute the previous noisy sample x_t -> x_{t-1} ##
                    member_latents = self.sample_noise_scheduler.step(noise_pred, t, member_latents).prev_sample
                avg_latents.append(member_latents)
            print('end sampling')
            avg_latents = torch.stack(avg_latents, dim=1)
            return avg_latents

    @torch.no_grad()
    def encode_stage(self, x):
        if utils.get_world_size() == 1 :
            z = self.model[list(self.model.keys())[1]].net.encode(x)
        else:
            z = self.model[list(self.model.keys())[1]].module.net.encode(x)
        return z.sample() * self.scale_factor

    @torch.no_grad()
    def decode_stage(self, z):
        z = z/self.scale_factor
        if utils.get_world_size() == 1 :
            z = self.model[list(self.model.keys())[1]].net.decode(z)
        else:
            z = self.model[list(self.model.keys())[1]].module.net.decode(z)
        return z

    @torch.no_grad()
    def init_scale_factor(self, z_tar):
        del self.scale_factor
        self.logger.info("### USING STD-RESCALING ###")
        _std = z_tar.std()
        if utils.get_world_size() == 1 :
            pass
        else:
            dist.barrier()
            dist.all_reduce(_std)
            _std = _std / dist.get_world_size()
        scale_factor = 1/_std
        self.logger.info(f'####### scale factor: {scale_factor.item()} ##########')
        self.register_buffer('scale_factor', scale_factor)

    def train_one_step(self, batch_data, step):
        data_dict = self.data_preprocess(batch_data)
        inp, tar = data_dict['inputs'], data_dict['data_samples'] 
        original_tar = data_dict['original_tar']
        
        b, t, c, h, w = tar.shape
        ## inp is coarse prediction in latent space, tar is gt in latent space
        with torch.no_grad():
            flat_tar = rearrange(tar, 'b t c h w -> (b t) c h w').contiguous()
            flat_inp = rearrange(inp, 'b t c h w -> (b t) c h w').contiguous()
            ## init scale_factor ##
            if self.scale_factor == 1.0:
                z_tar = self.encode_stage(flat_tar)
                z_tar = rearrange(z_tar, '(b t) c h w -> b t c h w', t=t).contiguous()
                self.init_scale_factor(z_tar)

            ## get z_prev_inp and z_tar ##
            z_tar = self.encode_stage(flat_tar)
            z_prev_inp = self.encode_stage(flat_inp)

            z_tar = rearrange(z_tar, '(b t) c h w -> b t c h w', t=t).contiguous()
            z_prev_inp = rearrange(z_prev_inp, '(b t) c h w -> b t c h w', b=b).contiguous()
        
        ## classifier free guidance ##
        p = torch.rand(1)
        if p < self.p_uncond: ## discard condition
            z_prev_inp_cond = torch.zeros_like(z_prev_inp)
        else:
            z_prev_inp_cond = z_prev_inp
        ## sample noise to add ##
        noise = torch.randn_like(z_tar)
        ## sample random timestep for each ##
        bs = inp.shape[0]
        timesteps = torch.randint(0, self.noise_scheduler.config.num_train_timesteps, (bs,), device=inp.device)
        noisy_tar = self.noise_scheduler.add_noise(z_tar, noise, timesteps)

        ## predict the noise residual ##
        noise_pred = self.model[list(self.model.keys())[0]](x=noisy_tar, timesteps=timesteps, cond=z_prev_inp_cond)

        loss = self.loss(noise_pred, noise) ## important: rescale the loss
        loss.backward()

        ## update params of diffusion model ##
        self.optimizer[list(self.model.keys())[0]].step()
        self.optimizer[list(self.model.keys())[0]].zero_grad()

        # if (utils.get_world_size() > 1 and mpu.get_data_parallel_rank() == 0) or utils.get_world_size() == 1:
        #             wandb.log({f'train_{self.loss_type}': loss.item() })
        if self.visualizer_type is None:
            pass
        elif self.visualizer_type == 'hko7_visualizer' and (step) % self.visualizer_step==0:
            # import pdb; pdb.set_trace() ##TODO sample image
            z_sample_prediction = self.denoise(template_data=z_tar, cond_data=z_prev_inp, bs=1)
            z_sample_prediction = rearrange(z_sample_prediction, 'b t c h w -> (b t) c h w').contiguous()
            sample_prediction = self.decode_stage(z_sample_prediction)
            sample_prediction = rearrange(sample_prediction, '(b t) c h w -> b t c h w', t=t)
            self.visualizer.save_pixel_image(pred_image=sample_prediction, target_img=tar, step=step)
        elif self.visualizer_type == 'sevir_visualizer' and (step) % self.visualizer_step==0:
            z_sample_prediction = self.denoise(template_data=z_tar, cond_data=z_prev_inp, bs=1)
            z_sample_prediction = rearrange(z_sample_prediction, 'b t c h w -> (b t) c h w').contiguous()
            sample_prediction = self.decode_stage(z_sample_prediction)
            sample_prediction = rearrange(sample_prediction, '(b t) c h w -> b t c h w', t=t) 
            self.visualizer.save_pixel_image(pred_image=sample_prediction, target_img=tar, step=step)
        elif self.visualizer_type == 'meteonet_visualizer' and (step) % self.visualizer_step==0:
            z_sample_prediction = self.denoise(template_data=z_tar, cond_data=z_prev_inp, bs=1)
            z_sample_prediction = rearrange(z_sample_prediction, 'b t c h w -> (b t) c h w').contiguous()
            sample_prediction = self.decode_stage(z_sample_prediction)
            sample_prediction = rearrange(sample_prediction, '(b t) c h w -> b t c h w', t=t) 
            self.visualizer.save_pixel_image(pred_image=sample_prediction, target_img=tar, step=step)
        else:
            pass
        return {self.loss_type: loss.item()}
    
        
    @torch.no_grad()
    def test_one_step(self, batch_data):
        data_dict = self.data_preprocess(batch_data)
        inp, tar = data_dict['inputs'], data_dict['data_samples'] 
        original_tar = data_dict['original_tar']
        b, t, c, h, w = tar.shape
        ## inp is coarse prediction in latent space, tar is gt in latent space
        with torch.no_grad():
            flat_tar = rearrange(tar, 'b t c h w -> (b t) c h w').contiguous()
            flat_inp = rearrange(inp, 'b t c h w -> (b t) c h w').contiguous()
            ## get z_prev_inp and z_tar ##
            z_tar = self.encode_stage(flat_tar)
            z_prev_inp = self.encode_stage(flat_inp)

            z_tar = rearrange(z_tar, '(b t) c h w -> b t c h w', t=t).contiguous()
            z_prev_inp = rearrange(z_prev_inp, '(b t) c h w -> b t c h w', b=b).contiguous()
        ## sample noise to add ##
        noise = torch.randn_like(z_tar)
        ## sample random timestep for each ##
        bs = inp.shape[0]
        timesteps = torch.randint(0, self.noise_scheduler.config.num_train_timesteps, (bs,), device=inp.device)
        noisy_tar = self.noise_scheduler.add_noise(z_tar, noise, timesteps)

        ## predict the noise residual ##
        noise_pred = self.model[list(self.model.keys())[0]](x=noisy_tar, timesteps=timesteps, cond=z_prev_inp)

        loss_records = {}
        ## evaluate other metrics ##
        data_dict = {}
        data_dict['gt'] = noise
        data_dict['pred'] = noise_pred
        MSE_loss = torch.mean((noise_pred - noise) ** 2).item()
        loss = self.loss(noise_pred, noise) ## important: rescale the loss

        ## evaluation ##
        if self.metrics_type == 'hko7_official':
            data_dict['gt'] = data_dict['gt'].squeeze(2).cpu().numpy()
            data_dict['pred'] = data_dict['pred'].squeeze(2).cpu().numpy()
            self.eval_metrics.update(gt=data_dict['gt'], pred=data_dict['pred'], mask=self.eval_metrics._exclude_mask)
            csi, mse, mae = self.eval_metrics.calculate_stat()
            for i, thr in enumerate(self.eval_metrics._thresholds):
                loss_records.update({f'CSI_{thr}': csi[:, i].mean()})
            loss_records.update({'MSE': MSE_loss})
        elif self.metrics_type == 'SEVIRSkillScore':
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
            # if (utils.get_world_size() > 1 and mpu.get_data_parallel_rank() == 0) or utils.get_world_size() == 1:
            #     wandb.log({f'val_CSI_m': loss_records['CSI_m'] })
        else:
            loss_records.update({'MSE': MSE_loss})
        
        ## log to wandb ##
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
        self.num_results2save = 0
        self.id_results2save = 0
        for step, batch in enumerate(data_loader):
            if self.debug and step>= 2 and self.sub_model_name[0] != "IDLE":
                break
            # if self.debug and step>= 2:
            #     break
            if isinstance(batch, int):
                batch = None

            loss = self.test_one_step(batch)
            metric_logger.update(**loss)

        self.logger.info('  '.join(
                [f'Epoch [{epoch + 1}](val stats)',
                 "{meters}"]).format(
                    meters=str(metric_logger)
                 ))

        return metric_logger
    
    @torch.no_grad()
    def eval_step(self, batch_data, step):
        data_dict = self.data_preprocess(batch_data)
        inp, tar = data_dict['inputs'], data_dict['data_samples'] 
        original_tar = data_dict['original_tar']

        b, t, c, h, w = tar.shape
        ## inp is coarse prediction in latent space, tar is gt in latent space
        flat_tar = rearrange(tar, 'b t c h w -> (b t) c h w').contiguous()
        flat_inp = rearrange(inp, 'b t c h w -> (b t) c h w').contiguous()
        ## get z_prev_inp and z_tar ##
        z_tar = self.encode_stage(flat_tar)
        z_prev_inp = self.encode_stage(flat_inp)

        z_tar = rearrange(z_tar, '(b t) c h w -> b t c h w', t=t).contiguous()
        z_prev_inp = rearrange(z_prev_inp, '(b t) c h w -> b t c h w', b=b).contiguous()

        ## sample image ##
        losses = {}
        z_sample_prediction = self.denoise(template_data=z_tar, cond_data=z_prev_inp, bs=tar.shape[0], vis=True, cfg=1.01, ensemble_member=1)
        len_shape_prediction = len(z_sample_prediction.shape)
        assert len_shape_prediction == 6
        n = z_sample_prediction.shape[1]
        sample_predictions = []
        for i in range(n):
            member_z_sample_prediction = z_sample_prediction[:, i, ...]
            member_z_sample_prediction = rearrange(member_z_sample_prediction, 'b t c h w -> (b t) c h w').contiguous()
            member_sample_prediction = self.decode_stage(member_z_sample_prediction)
            member_sample_prediction = rearrange(member_sample_prediction, '(b t) c h w -> b t c h w', t=t)
            sample_predictions.append(member_sample_prediction) 
        sample_predictions = torch.stack(sample_predictions, dim=1)
        ## evaluate other metrics ##
        data_dict = {}
        ## eval 384 #####
        if self.metrics_type == 'SEVIRSkillScore':
            sample_predictions = rearrange(sample_predictions, 'b k t c h w -> (b k t) c h w')
            sample_predictions = F.interpolate(sample_predictions, (original_tar.shape[-2], original_tar.shape[-1]), mode='bilinear', align_corners=True)
            sample_predictions = rearrange(sample_predictions, '(b k t) c h w -> b k t c h w', b=b, t=t)
            data_dict['gt'] =  original_tar
            data_dict['pred'] = sample_predictions.mean(dim=1)
            self.eval_metrics.update(target=data_dict['gt'], pred=data_dict['pred'])
            ############
            sf_dict = self.eval_metrics.get_single_frame_metrics(target=data_dict['gt'], pred=data_dict['pred'])
            crps_dict = self.eval_metrics.get_crps(target=data_dict['gt'], pred=sample_predictions)
            losses.update(sf_dict)
            losses.update(crps_dict)
        # # #### eval 128 ######
        # if self.metrics_type == 'SEVIRSkillScore':
        #     data_dict['gt'] =  tar
        #     data_dict['pred'] = sample_predictions.mean(dim=1)
        #     self.eval_metrics.update(target=data_dict['gt'], pred=data_dict['pred'])
        #     ############
        #     sf_dict = self.eval_metrics.get_single_frame_metrics(target=data_dict['gt'], pred=data_dict['pred'])
        #     crps_dict = self.eval_metrics.get_crps(target=data_dict['gt'], pred=sample_predictions)
        #     losses.update(sf_dict)
        #     losses.update(crps_dict)
        elif self.metrics_type == 'hko7_official':
            sample_predictions = rearrange(sample_predictions, 'b k t c h w -> (b k t) c h w')
            sample_predictions = F.interpolate(sample_predictions, (original_tar.shape[-2], original_tar.shape[-1]), mode='bilinear', align_corners=True)
            sample_predictions = rearrange(sample_predictions, '(b k t) c h w -> b k t c h w', b=b, t=t)
            data_dict['gt'] =  original_tar
            data_dict['pred'] = sample_predictions.mean(dim=1)
            self.eval_metrics.update(target=data_dict['gt'], pred=data_dict['pred'])
            ############
            sf_dict = self.eval_metrics.get_single_frame_metrics(target=data_dict['gt'], pred=data_dict['pred'])
            crps_dict = self.eval_metrics.get_crps(target=data_dict['gt'], pred=sample_predictions)
            losses.update(sf_dict)
            losses.update(crps_dict)
        elif self.metrics_type == 'METEONETScore':
            sample_predictions = rearrange(sample_predictions, 'b k t c h w -> (b k t) c h w')
            sample_predictions = F.interpolate(sample_predictions, (original_tar.shape[-2], original_tar.shape[-1]), mode='bilinear', align_corners=True)
            sample_predictions = rearrange(sample_predictions, '(b k t) c h w -> b k t c h w', b=b, t=t)
            data_dict['gt'] =  original_tar
            data_dict['pred'] = sample_predictions.mean(dim=1)
            self.eval_metrics.update(target=data_dict['gt'], pred=data_dict['pred'])
            ############
            sf_dict = self.eval_metrics.get_single_frame_metrics(target=data_dict['gt'], pred=data_dict['pred'])
            crps_dict = self.eval_metrics.get_crps(target=data_dict['gt'], pred=sample_predictions)
            losses.update(sf_dict)
            losses.update(crps_dict)
        ## save image ##
        if self.visualizer_type == 'sevir_visualizer' and (step) % 1 == 0:
            # self.visualizer.save_pixel_image(pred_image=data_dict['pred'], target_img=data_dict['gt'], step=step)
            # self.visualizer.save_npy(pred_image=data_dict['pred'], target_img=data_dict['gt'], step=step)
            ###########################################################################################################
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
        if self.metrics_type == 'SEVIRSkillScore':
            self.scale_factor = 0.5845165848731995 ## for 16x16x8
        elif self.metrics_type == 'hko7_official':
            self.scale_factor = 0.5686241984367371 ## for 16x16x8
        elif self.metrics_type == 'METEONETScore':
            self.scale_factor = 0.5848361849784851
        else:
            raise NotImplementedError
        
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

        from megatron_utils.tensor_parallel.data import get_data_loader_length
        total_step = get_data_loader_length(test_data_loader)

        from utils.metrics import cal_FVD
        self.fvd_computer = cal_FVD(use_gpu=True)

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
        ###################################################
        try: 
            metric_logger.update(**losses)
            self.logger.info('final results: {meters}'.format(meters=str(metric_logger)))
        except:
            ## save as excel ##
            import pandas as pd
            df = pd.DataFrame.from_dict(losses)
            df.to_excel(f'{self.visualizer.exp_dir}/{self.visualizer.sub_dir}_losses.xlsx')
        return None