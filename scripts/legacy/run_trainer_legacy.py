#!/usr/bin/env python
# coding=utf-8
# Copyright 2023 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Supervised fine-tuning script for decoder language models.
"""

import logging
import random
import sys

import datasets
import torch
import transformers
from transformers import AutoModelForCausalLM, set_seed, MistralModel, PhiModel
from transformers import TrainerCallback

from alignment import DataArguments, H4ArgumentParser, ModelArguments, SFTConfig, get_checkpoint, get_datasets
from alignment import get_VLA_dataset

from trl import SFTTrainer, DataCollatorForCompletionOnlyLM
import os

logger = logging.getLogger(__name__)

def main():
    try:
        print('MASTER_ADDR', os.environ['MASTER_ADDR'])
        print('MASTER_PORT', os.environ['MASTER_PORT'])
        print('NODE_RANK', os.environ['NODE_RANK'])
        print('LOCAL_RANK', os.environ['LOCAL_RANK'])
        print('RANK', os.environ['RANK'])
        print('WORLD_SIZE', os.environ['WORLD_SIZE'])
    except:
        pass

    from huggingface_hub import login
    login(token='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')

    parser = H4ArgumentParser((ModelArguments, DataArguments, SFTConfig))
    model_args, data_args, training_args = parser.parse()

    # Set seed for reproducibility
    set_seed(training_args.seed)

    ###############
    # Setup logging
    ###############
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    log_level = training_args.get_process_log_level()
    logger.setLevel(log_level)
    datasets.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.enable_default_handler()
    transformers.utils.logging.enable_explicit_format()

    # Log on each process a small summary
    logger.warning(
        f"Process rank: {training_args.local_rank}, device: {training_args.device}, n_gpu: {training_args.n_gpu}"
        + f" distributed training: {bool(training_args.local_rank != -1)}, 16-bits training: {training_args.fp16}"
    )
    logger.info(f"Model parameters {model_args}")
    logger.info(f"Data parameters {data_args}")
    logger.info(f"Training/evaluation parameters {training_args}")

    ################
    # Load tokenizer
    # The visual modality has 2048 (16384) tokens, and the action modality has 256 tokens, add them to the tokenizer
    # Add special tokens for the visual and action modalities, 
    #     including <bot_i>, <eot_i>, <bov_i>, <eov_i>, <boa_i>, <eoa_i>, <bov_o>, <eov_o>, <boa_o>, <eoa_o>
    # In total 2048 (16384) + 256 + 10 = 2314 (16650) tokens
    ################
    tokenizer = transformers.AutoTokenizer.from_pretrained(model_args.model_name_or_path)
    vocab_size = len(tokenizer)
    # add eos token when when calling tokenizer
    visual_tokens_to_add = ['<v' + str(i) + '>' for i in range(0, data_args.num_visual_tokens)]
    action_tokens_to_add = ['<a' + str(i) + '>' for i in range(0, data_args.num_action_tokens)]
    num_added_visual_tokens = tokenizer.add_special_tokens({'additional_special_tokens': visual_tokens_to_add})
    num_added_action_tokens = tokenizer.add_special_tokens({'additional_special_tokens': action_tokens_to_add})
    special_tokens = ['<bot_i>', '<eot_i>', '<bov_i>', '<eov_i>', '<boa_i>', '<eoa_i>', 
                            '<bov_o>', '<eov_o>', '<boa_o>', '<eoa_o>']
    num_added_special_tokens = tokenizer.add_special_tokens({'additional_special_tokens': special_tokens})
    tokenizer.add_special_tokens({'pad_token': '[PAD]'})
    # For SFT training, padding should be on the right
    tokenizer.padding_side = data_args.padding_side

    #######################
    # Load and pre-process the dataset
    #######################

    train_dataset = get_VLA_dataset(data_args, vocab_size, split='train')
    eval_dataset = get_VLA_dataset(data_args, vocab_size, split='test')

    # only take a little samples for debug
    if training_args.debug:
        print('Debug mode, only take a little samples for training and evaluation')
        train_dataset = train_dataset.select(range(2000))
        eval_dataset = eval_dataset.select(range(100))
    
    with training_args.main_process_first(desc="Log a few random samples from the processed training set"):
        index = random.randint(0, len(train_dataset))
        logger.info(f"Sample {index} from the training set:\n\n{train_dataset[index]}")

    def preprocess_func(example): 
        '''
        Format the example into a sequence format
        examples is a dict with the following keys:
        - text: text prompt of the manipulation task, in natural language
            since max sequence length is 2048, its max number of tokens is 2048 - 12 - 6*256 - 6*7 - 256 - 7 = 195
        - input_visual: input visual tokens for the manipulation task, in token format, e.g., <v1> <v2> <v3>
        - input_action: input action tokens for the manipulation task, in token format
        - output_visual: output visual tokens for the manipulation task, in token format
        - output_action: output action tokens for the manipulation task, in token format
        sequence format: bos + bot_i + text + eot_i +
                        bov_i + input_visual + eov_i +
                        boa_i + input_action + eoa_i + 
                        bov_o + output_visual + eov_o +
                        boa_o + output_action + eoa_o + eos (padding will be automatically added later by the trainer)
        '''
        
        example['text'] = '<bot_i>' + example['text'] + '<eot_i>' + \
                    '<bov_i>' + ''.join(tokenizer.convert_ids_to_tokens(example['input_visual'])) + '<eov_i>' + \
                    '<boa_i>' + ''.join(tokenizer.convert_ids_to_tokens(example['input_action'])) + '<eoa_i>' + \
                    '<bov_o>' + ''.join(tokenizer.convert_ids_to_tokens(example['output_visual'])) + '<eov_o>' + \
                    '<boa_o>' + ''.join(tokenizer.convert_ids_to_tokens(example['output_action'])) + '<eoa_o>' + \
                    tokenizer.eos_token

        return example

    train_dataset = train_dataset.map(
        preprocess_func,
        num_proc=data_args.preprocessing_num_workers,
        remove_columns=['input_visual', 'output_visual', 'input_action', 'output_action'],
        desc="Preprocessing training dataset",
    )
    eval_dataset = eval_dataset.map(
        preprocess_func,
        num_proc=data_args.preprocessing_num_workers,
        remove_columns=['input_visual', 'output_visual', 'input_action', 'output_action'],
        desc="Preprocessing testing dataset",
    )

    with training_args.main_process_first(desc="Log a few random samples from the processed training set"):
        index = random.randint(0, len(train_dataset))
        logger.info(f"Sample {index} from the training set:\n\n{train_dataset[index]}")
    
    # input always ends by <eoa_i>, use <eoa_i> as the response template
    response_template_id = tokenizer.convert_tokens_to_ids(['<eoa_i>'])
    data_collator = DataCollatorForCompletionOnlyLM(response_template_id, tokenizer=tokenizer)

    #######################
    # Load pretrained model
    #######################
    logger.info("*** Load pretrained model ***")
    # use float16 (V100 does not support bfloat16)
    torch_dtype = torch.float16 if training_args.fp16 else torch.float32

    model_kwargs = dict(
        revision=model_args.model_revision,
        trust_remote_code=model_args.trust_remote_code,
        use_flash_attention_2=model_args.use_flash_attention_2,
        torch_dtype=torch_dtype,
        use_cache=False if training_args.gradient_checkpointing else True
    )

    model = AutoModelForCausalLM.from_pretrained(
        model_args.model_name_or_path,
        **model_kwargs,
    )
    model.resize_token_embeddings(len(tokenizer), pad_to_multiple_of=128) # now we have 32000 + 2314 = 34314 tokens, pad to multiple of 128 to improve performance
    # TODO: use pre-trained features to initialize the visual and action embeddings

    ########################
    # Initialize the Trainer
    ########################

    class PrintCallback(TrainerCallback):
        def on_evaluation(self, args: transformers.TrainingArguments, state: transformers.TrainerState, control: transformers.TrainerControl, **kwargs):
            # print whether this process should save the checkpoint
            print(f'Process {args.local_rank} should save checkpoint: {args.should_save}')

    trainer = SFTTrainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        tokenizer=tokenizer,
        dataset_text_field="text",
        data_collator=data_collator,
        callbacks=[PrintCallback()],
        max_seq_length=training_args.max_seq_length,
        dataset_kwargs=training_args.dataset_kwargs,
    )

    ###############
    # Training loop
    ###############

    # Check for last checkpoint
    last_checkpoint = get_checkpoint(training_args)
    if last_checkpoint is not None and training_args.resume_from_checkpoint is None:
        logger.info(f"Checkpoint detected, resuming training at {last_checkpoint}.")

    logger.info("*** Train ***")
    checkpoint = None
    if training_args.resume_from_checkpoint is not None:
        checkpoint = training_args.resume_from_checkpoint
    elif last_checkpoint is not None:
        checkpoint = last_checkpoint
    train_result = trainer.train(resume_from_checkpoint=checkpoint)
    metrics = train_result.metrics
    metrics["train_samples"] = len(train_dataset)
    trainer.log_metrics("train", metrics)
    trainer.save_metrics("train", metrics)
    trainer.save_state()

    ##################################
    # Save model and create model card
    ##################################
    logger.info("*** Save model ***")
    trainer.save_model(training_args.output_dir)
    logger.info(f"Model saved to {training_args.output_dir}")

    # Save everything else on main process
    kwargs = {
        "finetuned_from": model_args.model_name_or_path,
        "dataset": list(data_args.dataset_mixer.keys()),
        "dataset_tags": list(data_args.dataset_mixer.keys()),
        "tags": ["alignment-handbook"],
    }
    if trainer.accelerator.is_main_process:
        trainer.create_model_card(**kwargs)
        # Restore k,v cache for fast inference
        trainer.model.config.use_cache = True
        trainer.model.config.save_pretrained(training_args.output_dir)

    ##########
    # Evaluate
    ##########
    if training_args.do_eval:
        logger.info("*** Evaluate ***")
        metrics = trainer.evaluate()
        metrics["eval_samples"] = len(eval_dataset)
        trainer.log_metrics("eval", metrics)
        trainer.save_metrics("eval", metrics)

    logger.info("*** Training complete ***")


if __name__ == "__main__":
    main()
