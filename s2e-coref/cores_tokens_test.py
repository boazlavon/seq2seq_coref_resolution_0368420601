import json
import argparse
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
from conll import evaluate_conll

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
def get_cores_tokens():
    cores_tokens  = [STARTING_TOKEN, ENDING_TOKEN] # starting ending of a mention
    cores_tokens += [UNK_CLUSTER_TOKEN, IN_CLUSTER_TOKEN, NOT_IN_CLUSTER_TOKEN] # whether mentino is inside the cluster or not. TODO: with and without F token
    # I will tag the color after the ending token. therefore the decoder can context everything it saw. all the previous taggings and mentions the all the current mention and decide about the color.
    return cores_tokens


class CoresDatasetPreProcessorTest(object):
    def __init__(self, test_data_path, tokenizer, max_seq_length=-1, batch_size=1):
        self.batch_size = batch_size
        self.max_seq_length = max_seq_length

        self.tokenizer = tokenizer
        print(f"Reading dataset from {test_data_path}")
        self.original_examples, self.max_mention_num, self.max_cluster_size, self.max_num_clusters = self._parse_jsonlines(test_data_path)
        self.paragraph_examples, self.mentions_examples = self._split_to_paragraphs(self.original_examples)
        self.coref_examples = self._tokenize()
        _, self.cluster_examples = self._binary_clustering_tokenize(self.mentions_examples)

    def _binary_clustering_tokenize(self, examples):
        cluster_examples = []
        num_examples_filtered = 0
        #for (idx, paragraph_id, words, clusters) in examples:
        for (idx, doc_key, paragraph_id, words, clusters, _) in self.mentions_examples:
            words = flatten_list_of_lists(words)
            
            current_cluster_examples = []
            mentions = sum([len(c) for c in clusters])
            for c_i, cluster in enumerate(clusters):
                try:
                    cluster_output_str = encode(words, clusters, cluster_tag=c_i, mention_tag=None)
                    cluster_output_str = ' '.join(cluster_output_str)
                    cluster_output   = self.tokenizer(cluster_output_str, padding="max_length")
                    output_ids       = cluster_output['input_ids']
                    output_ids       = torch.tensor(output_ids).unsqueeze(0)
                    output_ids_mask  = cluster_output['attention_mask']
                    output_ids_mask  = torch.tensor(output_ids_mask).unsqueeze(0)
                except:
                    num_examples_filtered += len(cluster)
                    continue
                    
                if 0 < self.max_seq_length < output_ids.shape[1]:
                    num_examples_filtered += len(cluster)
                    continue

                for mention in cluster:
                    try:
                        mention_input_str   = encode(words, clusters, cluster_tag=c_i, mention_tag=mention)
                        mention_input_str   = ' '.join(mention_input_str)
                        mention_input   = self.tokenizer(mention_input_str, padding="max_length")
                        input_ids       = mention_input['input_ids']
                        input_ids       = torch.tensor(input_ids).unsqueeze(0)
                        input_ids_mask  = mention_input['attention_mask']
                        input_ids_mask  = torch.tensor(input_ids_mask).unsqueeze(0)
                    except Exception as e:
                        num_examples_filtered += 1
                        continue

                    if 0 < self.max_seq_length < input_ids.shape[1]:
                        num_examples_filtered += 1
                        continue

                    current_cluster_examples.append((doc_key, idx, paragraph_id, c_i, mention, mention_input_str, cluster_output_str))
            print(f"clusters: idx = {idx} paragraph_id = {paragraph_id} mention_examples = {len(current_cluster_examples)} / {mentions}")
            cluster_examples.extend(current_cluster_examples)
        return num_examples_filtered, cluster_examples

    def to_ontonotes(self, ontonotes_path):
        with open(ontonotes_path, 'w') as f:
            for i, (idx, doc_key, paragraph_id, sentences, clusters, _, conll_lines, _) in enumerate(self.paragraph_examples):
                # beggining line
                if i > 0:
                    f.write('\n')
                beginning = f'#begin document ({doc_key}); part {paragraph_id}\n'
                f.write(beginning)
                assert [len(sentence) for sentence in sentences] == [len(lines_group) for lines_group in conll_lines]
                # iterate over a sentence
                for sentence_idx, (sentence, lines_group) in enumerate(zip(sentences, conll_lines)):
                    for word_idx, (word, line) in enumerate(zip(sentence, lines_group)):
                        new_line = ''
                        row = line.split()
                        assert doc_key == f'{row[0]}_{row[1]}'
                        new_line += doc_key
                        new_line += (4 - len(str(paragraph_id))) * ' '
                        new_line += str(paragraph_id)
                        new_line += (5 - len(str(word_idx))) * ' '
                        new_line += str(word_idx)
                        new_line += line[29:]
                        f.write(new_line)
                    f.write('\n')
                f.write('#end document')

    def unite_paragraph_clusters(self):
        united_clusters = {}
        for i, (idx, doc_key, paragraph_id, sentences, clusters, _, conll_lines, index_shift) in enumerate(self.paragraph_examples):
            orig_index_clusters = [[start + index_shift, end + index_shift] for start, end in cluster for cluster in clusters ]
            if not doc_key in united_clusters:
                united_clusters[doc_key] = []
            united_clusters.extend(orig_index_clusters)
        return united_clusters

    def _tokenize(self):
        coref_examples = {}

        #for doc_key, words, clusters, speakers in examples:
        for _, doc_key, paragraph_id, sentences, clusters, sentences_speakers, _, _ in self.paragraph_examples:
            words = flatten_list_of_lists(sentences)
            speakers = flatten_list_of_lists(sentences_speakers)

            word_idx_to_start_token_idx = dict()
            word_idx_to_end_token_idx = dict()
            end_token_idx_to_word_idx = [0]  # for <s>

            token_ids = []
            last_speaker = None
            for idx, (word, speaker) in enumerate(zip(words, speakers)):
                if last_speaker != speaker:
                    speaker_prefix = [SPEAKER_START] + self.tokenizer.encode(" " + speaker,
                                                                             add_special_tokens=False) + [SPEAKER_END]
                    last_speaker = speaker
                else:
                    speaker_prefix = []
                for _ in range(len(speaker_prefix)):
                    end_token_idx_to_word_idx.append(idx)
                token_ids.extend(speaker_prefix)
                word_idx_to_start_token_idx[idx] = len(token_ids) + 1  # +1 for <s>
                tokenized = self.tokenizer.encode(" " + word, add_special_tokens=False)
                for _ in range(len(tokenized)):
                    end_token_idx_to_word_idx.append(idx)
                token_ids.extend(tokenized)
                word_idx_to_end_token_idx[idx] = len(token_ids)  # old_seq_len + 1 (for <s>) + len(tokenized_word) - 1 (we start counting from zero) = len(token_ids)
            new_clusters = [
                [(word_idx_to_start_token_idx[start], word_idx_to_end_token_idx[end]) for start, end in cluster] for
                cluster in clusters]

            coref_examples[(doc_key, paragraph_id)] = (end_token_idx_to_word_idx, token_ids, new_clusters, 
                                                       word_idx_to_start_token_idx, word_idx_to_end_token_idx)
            for idx, word in enumerate(words):
                x = word_idx_to_start_token_idx[idx]
                x = word_idx_to_end_token_idx[idx]
        return coref_examples

    def _parse_jsonlines(self, test_data_path):
        examples = {}
        max_mention_num = -1
        max_cluster_size = -1
        max_num_clusters = -1
        with open(test_data_path, 'r') as f:
            for line in f:
                d = json.loads(line.strip())
                doc_key = d["doc_key"]
                input_words = d["sentences"]
                clusters = d["clusters"]
                speakers = d["speakers"]
                try:
                    conll_lines = d["full_sentences"]
                except:
                    conll_lines = 'EMPTY!'
                max_mention_num = max(max_mention_num, len(flatten_list_of_lists(clusters)))
                max_cluster_size = max(max_cluster_size, max(len(cluster) for cluster in clusters) if clusters else 0)
                max_num_clusters = max(max_num_clusters, len(clusters) if clusters else 0)
                examples[doc_key] = (input_words, clusters, speakers, conll_lines)

        return examples, max_mention_num, max_cluster_size, max_num_clusters

    def _trunc_words(self, words, clusters, trunc_sentences_count):
        if trunc_sentences_count > 0:
            w = words[:-trunc_sentences_count]
        else:
            w = words
        w_flatten = flatten_list_of_lists(w)

        new_clusters = [ [[start, end] for start, end in cluster if start < len(w_flatten) and end < len(w_flatten)] for cluster in clusters ]
        new_clusters = [ cluster for cluster in new_clusters if cluster ]
        mentions_count = sum([len(c) for c in new_clusters])
        #print(f'len(w) = {len(w)}, len(w_flatten) = {len(w_flatten)}, mentions_count = {mentions_count}')
        return w, new_clusters

    def _process_example(self, words, clusters, trunc_step=1):
        w = list(words)
        trunc_sentences_count = 0
        total_mention_count = sum([len(c) for c in clusters])
        while w:
            w, new_clusters  = self._trunc_words(list(words), clusters, trunc_sentences_count)
            w_flatten        = flatten_list_of_lists(w)
            words_str        = ' '.join(w_flatten)
            tokenized_input  = self.tokenizer(words_str, padding="max_length") # padding
            input_ids        = tokenized_input['input_ids']
            input_ids        = torch.tensor(input_ids).unsqueeze(0)
            input_ids_mask   = tokenized_input['attention_mask']
            input_ids_mask   = torch.tensor(input_ids_mask).unsqueeze(0)

            entity_mentions = encode(w_flatten, new_clusters, None)
            entity_mentions = ' '.join(entity_mentions)
            tokenized_output = self.tokenizer(entity_mentions, padding="max_length")
            output_ids       = tokenized_output['input_ids']
            output_ids       = torch.tensor(output_ids).unsqueeze(0)
            output_ids_mask  = tokenized_output['attention_mask']
            output_ids_mask  = torch.tensor(output_ids_mask).unsqueeze(0)

            #output_str = self.tokenizer.batch_decode(output_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)
            if 0 < self.max_seq_length < input_ids.shape[1]:
                trunc_sentences_count += trunc_step
                continue
                
            if 0 < self.max_seq_length < output_ids.shape[1]:
                trunc_sentences_count += trunc_step
                continue
            break
            print('Done')
        return w, words_str, new_clusters, trunc_sentences_count, entity_mentions

    def _split_to_paragraphs(self, examples):
        paragraph_examples = []
        mentions_examples = []
        idx = 0
        for doc_key, (words, clusters, speakers, conll_lines) in examples.items():
            paragraph_id = 0
            index_shift = 0
            total_length = len(flatten_list_of_lists(words))
            new_words, words_str, new_clusters, trunc_sentences_count, entity_mentions = self._process_example(words, clusters)
            new_speakers = speakers[:len(new_words)]
            new_conll_lines = conll_lines[:len(new_words)]
            paragraph_examples.append((idx, doc_key, paragraph_id, new_words, new_clusters, new_speakers, new_conll_lines, index_shift))
            mentions_examples.append((idx, doc_key, paragraph_id, new_words, new_clusters, entity_mentions))
            trunced_length = len(new_words)
            print(f"mention: idx = {idx} doc_key = {doc_key} paragraph_id = {paragraph_id} sentences={trunced_length}/{len(words)} shift = {index_shift} / {total_length}")
            while trunced_length <  len(words):
                remain_words       = words[trunced_length:]
                remain_speakers    = speakers[trunced_length:]
                remain_conll_lines = conll_lines[trunced_length:]

                full_trunced_length = len(flatten_list_of_lists(new_words))
                index_shift += full_trunced_length # the length of the sentence so it will be possible to restore the indexes to original sentence

                remain_clusters = [ [[start - full_trunced_length , end - full_trunced_length] for start, end in cluster \
                                  if start >= full_trunced_length and end >= full_trunced_length] for cluster in clusters ]
                remain_clusters = [ cluster for cluster in remain_clusters if cluster ]

                new_words, words_str, new_clusters, trunc_sentences_count, entity_mentions = self._process_example(remain_words, remain_clusters)
                new_speakers = remain_speakers[:len(new_words)]
                new_conll_lines = remain_conll_lines[:len(new_words)]
                
                paragraph_id += 1
                trunced_length += len(new_words)
                if new_clusters:
                    paragraph_examples.append((idx, doc_key, paragraph_id, new_words, new_clusters, new_speakers, new_conll_lines, index_shift))
                    mentions_examples.append((idx, doc_key, paragraph_id, new_words, new_clusters, entity_mentions))
                    print(f"mention: idx = {idx} doc_key = {doc_key} paragraph_id = {paragraph_id} sentences={trunced_length}/{len(words)} shift = {index_shift} / {total_length}")
                else:
                    print(f"IGNORED! mention: idx = {idx} doc_key = {doc_key} paragraph_id = {paragraph_id} sentences={trunced_length}/{len(words)} shift = {index_shift} / {total_length}")

                if not new_words:
                    print(f"No New Words!")
                    break

                if new_words == remain_words:
                    print(f"Finished all words")
                    break
            idx += 1

        return paragraph_examples, mentions_examples


    def evaluate(self, inference_dir, official=True):
        mention_evaluator = MentionEvaluator()
        coref_evaluator = CorefEvaluator()
        doc_to_prediction = {}
        doc_to_subtoken_map = {}
        output_dir = os.path.join(inference_dir, 'eval_output')
        try:
            os.mkdir(output_dir)
        except:
            pass
        conll_golden_path = os.path.join(output_dir, 'eval_conll_gold_path')
        self.to_ontonotes(conll_golden_path)

        #for (doc_key, subtoken_maps), batch in eval_dataloader:
        for _, doc_key, paragraph_id, sentences, untokenized_golden_clusters, _, _ in self.paragraph_examples:
            subtoken_maps, _, gold_clusters, word_idx_to_start_token_idx, word_idx_to_end_token_idx = self.coref_examples[(doc_key, paragraph_id)]
            gold_clusters = tuple([tuple(c) for c in gold_clusters])

            mention_to_gold_clusters = extract_mentions_to_predicted_clusters_from_clusters(gold_clusters)
            gold_mentions = tuple(mention_to_gold_clusters.keys())

            # predict_clusters = load from file by doc_key and paragraph_id
            # inference_dir
            doc_key_file = doc_key.replace('/', '\\')
            inference_results = os.path.join(inference_dir, f'{doc_key_file}_{paragraph_id}.pkl')
            try:
                with open(inference_results, 'rb') as f:
                    _, _, untok_predicted_clusters = pickle.load(f)
            except:
                print(f'{inference_results} dont exist. continue!')
                continue

            # tokenize predict_clusters with map.
            predicted_clusters = [ [(word_idx_to_start_token_idx[start], word_idx_to_end_token_idx[end]) for start, end in cluster] for
                                     cluster in untok_predicted_clusters ]

            predicted_clusters = tuple([tuple(c) for c in predicted_clusters])
            mention_to_predicted_clusters = extract_mentions_to_predicted_clusters_from_clusters(predicted_clusters)
            predicted_mentions = tuple(mention_to_predicted_clusters.keys())

            mention_evaluator.update(predicted_mentions, gold_mentions)
            coref_evaluator.update(predicted_clusters, gold_clusters, mention_to_predicted_clusters, mention_to_gold_clusters)
            doc_to_prediction[f'{doc_key}_{paragraph_id}'] = predicted_clusters
            doc_to_subtoken_map[f'{doc_key}_{paragraph_id}'] = subtoken_maps
            #print(f'{doc_key}_{paragraph_id} succes!')

        mention_precision, mentions_recall, mention_f1 = mention_evaluator.get_prf()
        prec, rec, f1 = coref_evaluator.get_prf()

        results = [
            ("mention precision", mention_precision),
            ("mention recall", mentions_recall),
            ("mention f1", mention_f1),
            ("precision", prec),
            ("recall", rec),
            ("f1", f1)
        ]
        print("***** Eval results {} *****")
        for key, values in results:
            if isinstance(values, float):
                print(f"  {key} = {values:.3f}")
            else:
                print(f"  {key} = {values}")

        if official:
            with open(os.path.join(output_dir, "preds.jsonl"), "w") as f:
                f.write(json.dumps(doc_to_prediction) + '\n')
                f.write(json.dumps(doc_to_subtoken_map) + '\n')

            conll_results = evaluate_conll(conll_golden_path, doc_to_prediction, doc_to_subtoken_map)
            official_f1 = sum(results["f"] for results in conll_results.values()) / len(conll_results)
            print('Official avg F1: %.4f' % official_f1)
        return results

    def __len__(self):
        return len(self.examples)


def create_datasets():
    # path
    model_type = sys.argv[1]
    if model_type not in ('bert', 't5', 'bart'):
        print('Invalid Model Type')
        sys.exit(0)

    test_data_path = sys.argv[2]
    if not os.path.exists(test_data_path):
        print(f'Path dont exists {test_data_path}')
        sys.exit(0)

    print("Loading tokenizer")
    if model_type == 'bert':
        tokenizer = BertTokenizerFast.from_pretrained("bert-base-uncased")
        tokenizer.bos_token = tokenizer.cls_token
        tokenizer.eos_token = tokenizer.sep_token
    if model_type == 't5':
        tokenizer = T5Tokenizer.from_pretrained("t5-small")
    cores_tokens = ['<<', '>>', '[[u]]', '[[t]]', '[[f]]']
    tokenizer.add_tokens(cores_tokens)
    tokenizer.model_max_length = 128

    filename = os.path.basename(test_data_path)
    builders_dir = os.path.join(r'.', 'new_builders')
    try:
        os.mkdir(builders_dir)
    except:
        pass
    dataset_builder_path = os.path.join(builders_dir, f'{filename}.builder.{model_type}.pkl')
    print(f'Builder path: {dataset_builder_path}')

    if os.path.exists(dataset_builder_path):
        with open(dataset_builder_path, 'rb') as f:
            builder = pickle.load(f)
        print(f"Loaded Builder: {dataset_builder_path}")
    else:
        builder = CoresDatasetPreProcessorTest(test_data_path, tokenizer, max_seq_length=128)
        with open(dataset_builder_path, 'wb') as f:
            pickle.dump(builder, f)
        print(f"Saved Builder: {dataset_builder_path}")

def load_pickles():
    os.environ["PYTHONUNBUFFERED"] = '1'
    dataset_builder_path = sys.argv[1]
    print(f'Builder path: {dataset_builder_path}')
    if not os.path.exists(dataset_builder_path):
        print(f'Please generate builder (cores_tokens_test.py script): {dataset_builder_path}')
        sys.exit(0)

    print("Loading Builder")
    with open(dataset_builder_path, 'rb') as f:
        builder = pickle.load(f)
    
    infer_dir = sys.argv[2]
    if not os.path.isdir(infer_dir):
        print('Please provide an inference directory')
    return builder, ifer_dir

def generate_true_cluster_example(true_mention, sentence, model_output_mentions):
    for m in model_output_mentions:
        replace_tok = UNK_CLUSTER_TOKEN
        if m[MEN_SPAN_RANGE_IDX] == true_mention[MEN_SPAN_RANGE_IDX]:
            replace_tok = IN_CLUSTER_TOKEN
        start, end = m[MEN_SPAN_RANGE_IDX]
        full_mention = sentence[start: end + 1]
        if f'[[{m[MEN_CLUSTER_TAG_IDX]}]]' == replace_tok:
            continue
        replaced_full_mention = full_mention.replace(f'[[{m[MEN_CLUSTER_TAG_IDX]}]]', replace_tok)
        if (full_mention != replaced_full_mention):
            print(f'Replace!: {m}')
            print(full_mention)
            print(replaced_full_mention)
            sentence = sentence.replace(full_mention, replaced_full_mention)
        return sentence

if __name__ == '__main__':
    create_datasets()
