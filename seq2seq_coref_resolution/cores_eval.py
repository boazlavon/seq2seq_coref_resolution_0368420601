import json
import subprocess, os, sys
import  argparse
import random
import logging
import os
import pickle
import time, threading, sys

import datasets
from metrics import CorefEvaluator, MentionEvaluator
from datasets import Dataset, concatenate_datasets
from datasets import Dataset, load_metric
from utils import extract_mentions_to_predicted_clusters_from_clusters
from cores_tokens import encode
from consts import SPEAKER_START, SPEAKER_END, NULL_ID_FOR_COREF
from cores_tokens_test import CoresDatasetPreProcessorTest

import torch
import pandas as pd

# Training imports
from transformers import AutoConfig, AutoTokenizer, CONFIG_MAPPING, LongformerConfig, BertConfig, BertTokenizer, BertTokenizerFast
from transformers import BertTokenizerFast
from transformers import T5Tokenizer
from transformers import BertGenerationConfig, BertGenerationEncoder, BertGenerationDecoder, EncoderDecoderModel, EncoderDecoderConfig
from transformers import Seq2SeqTrainingArguments, Seq2SeqTrainer
from utils import flatten_list_of_lists

STARTING_TOKEN = '<<'
ENDING_TOKEN = '>>'
UNK_CLUSTER_TOKEN = '[[u]]'
IN_CLUSTER_TOKEN = '[[t]]'
NOT_IN_CLUSTER_TOKEN = '[[f]]'

logger = logging.getLogger(__name__)

def load_pickles(dataset_builder_path):
    os.environ["PYTHONUNBUFFERED"] = '1'
    print(f'Builder path: {dataset_builder_path}')
    if not os.path.exists(dataset_builder_path):
        print(f'Please generate builder (cores_tokens_test.py script): {dataset_builder_path}')
        sys.exit(0)

    print("Loading Builder")
    with open(dataset_builder_path, 'rb') as f:
        builder = pickle.load(f)

    return builder

def eval():
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument('--model', type=str)
    parser.add_argument('--builder', type=str)
    parser.add_argument('--beam', type=int)
    parser.add_argument('--dropout', type=float)
    parser.add_argument('--official', type=bool, default=True)
    parser.add_argument('--tag_only_clusters', type=bool, default=False)
    args = parser.parse_args(sys.argv[1:])
    config = f'{args.dropout}'
    infer_config = config
    if args.tag_only_clusters:
        infer_config = f'{args.dropout}_clusters_prediction_only'

    proj_dir = r'.'
    infer_main_dir = os.path.join(proj_dir, 'inference_results')
    infer_dir = os.path.join(infer_main_dir, args.model, infer_config, f'beam_{args.beam}')
    if not os.path.isdir(infer_dir):
        print('Please provide an inference directory: {infer_dir}')

    proj_dir = r'.'
    eval_main_dir = os.path.join(proj_dir, 'eval_results')
    eval_dir = os.path.join(eval_main_dir, args.model, infer_config, f'beam_{args.beam}')
    eval_output_dir = os.path.join(eval_dir, 'eval_output')
    try:
        os.system(f'mkdir -p {eval_output_dir}')
    except:
        pass

    print(f'Eval output dir: {eval_output_dir}')
    paragraph_output_dir = os.path.join(eval_output_dir, 'paragrph_eval_output')
    try:
        os.mkdir(paragraph_output_dir)
    except:
        pass
    documents_output_dir = os.path.join(eval_output_dir, 'document_eval_output')
    try:
        os.mkdir(documents_output_dir)
    except:
        pass

    builder = load_pickles(args.builder)
    paragraph_eval_results = os.path.join(paragraph_output_dir, 'eval.log')
    tee = subprocess.Popen(["tee", paragraph_eval_results], stdin=subprocess.PIPE)
    os.dup2(tee.stdin.fileno(), sys.stdout.fileno())
    os.dup2(tee.stdin.fileno(), sys.stderr.fileno())
    print('****************************************************')
    print('******************  Paragraph Eval ******************')
    builder.paragraphs_evaluate(infer_dir, paragraph_output_dir, official=args.official)
    print('****************************************************')

    document_eval_results = os.path.join(documents_output_dir, 'eval.log')
    tee = subprocess.Popen(["tee", document_eval_results], stdin=subprocess.PIPE)
    os.dup2(tee.stdin.fileno(), sys.stdout.fileno())
    os.dup2(tee.stdin.fileno(), sys.stderr.fileno())
    print('******************  Document Eval ******************')
    builder.documents_evaluate(infer_dir, documents_output_dir, official=args.official)
    print('****************************************************')

if __name__ == '__main__':
    eval()
