import argparse
from collections import Counter
import json
import os
import time

import numpy as np
import pandas as pd
import tiktoken
from tqdm import tqdm
from sglang.test.test_utils import add_common_sglang_args_and_parse, select_sglang_backend


choices = ["A", "B", "C", "D"]

tokenizer = tiktoken.encoding_for_model("gpt-3.5-turbo")


def format_subject(subject):
    l = subject.split("_")
    s = ""
    for entry in l:
        s += " " + entry
    return s

def format_example(df, idx, include_answer=True):
    prompt = df.iloc[idx, 0]
    k = df.shape[1] - 2
    for j in range(k):
        prompt += "\n{}. {}".format(choices[j], df.iloc[idx, j+1])
    prompt += "\nAnswer:"
    if include_answer:
        prompt += " {}\n\n".format(df.iloc[idx, k + 1])
    return prompt

def gen_prompt(train_df, subject, k=-1):
    prompt = "The following are multiple choice questions (with answers) about{}.\n\n".format(format_subject(subject))
    if k == -1:
        k = train_df.shape[0]
    for i in range(k):
        prompt += format_example(train_df, i)
    return prompt

def evaluate(args, subject, dev_df, test_df):
    prompts = []
    labels = []

    k = args.ntrain
    few_shot_examples = gen_prompt(dev_df, subject, k)
    while len(tokenizer.encode(few_shot_examples)) > 1536:
        k -= 1
        few_shot_examples = gen_prompt(dev_df, subject, k)

    for i in range(test_df.shape[0]):
        prompt_end = format_example(test_df, i, include_answer=False)
        prompts.append(prompt_end)

        label = test_df.iloc[i, test_df.shape[1]-1]
        labels.append(label)

    arguments = [{"question": p} for p in prompts]

    #####################################
    ######### SGL Program Begin #########
    #####################################

    import sglang as sgl

    if args.backend.startswith("gpt-") or args.backend.startswith("router-"):
        @sgl.function
        def few_shot_mmlu(s, examples, question):
            s += sgl.user(examples + question)
            s += sgl.assistant(sgl.gen("answer"))
    else:
        @sgl.function
        def few_shot_mmlu(s, examples, question):
            s += examples + question + sgl.gen("answer")

    #####################################
    ########## SGL Program End ##########
    #####################################

    # Select backend
    backend = select_sglang_backend(args)

    tic = time.time()
    states = few_shot_mmlu.bind(examples=few_shot_examples).run_batch(
        arguments, temperature=0, max_new_tokens=1,
        backend=backend, num_threads=args.parallel)
    preds = [s["answer"].strip()[0] if len(s["answer"].strip()) > 0 else ""
             for s in states]
    models = [s["model"] for s in states]
    latency = time.time() - tic

    cors = [pred == label for pred, label in zip(preds, labels)]
    acc = np.mean(cors)
    cors = np.array(cors)
    model_counts = Counter(models)

    print("Average accuracy {:.3f}, latency {:.2f}, #q: {} - {}, routing: {}".format(
        acc, latency, len(prompts), subject, ", ".join([f"{k}: {v}" for k, v in model_counts.items()])))

    return cors, acc, latency, model_counts


def main(args):
    subjects = sorted([f.split("_test.csv")[0] for f in os.listdir(os.path.join(args.data_dir, "test")) if "_test.csv" in f])

    all_cors = []
    all_latencies = []
    all_model_counts = Counter()
    num_requests = 0

    for subject in tqdm(subjects[:args.nsub]):
        dev_df = pd.read_csv(os.path.join(args.data_dir, "dev", subject + "_dev.csv"), header=None)[:args.ntrain]
        test_df = pd.read_csv(os.path.join(args.data_dir, "test", subject + "_test.csv"), header=None)

        cors, acc, latency, model_counts = evaluate(args, subject, dev_df, test_df)
        all_cors.append(cors)
        all_latencies.append(latency)
        all_model_counts.update(model_counts)
        num_requests += len(test_df)

    total_latency = np.sum(all_latencies)
    print("Total latency: {:.3f}".format(total_latency))

    weighted_acc = np.mean(np.concatenate(all_cors))
    print("Average accuracy: {:.3f}".format(weighted_acc))

    print(f"Model counts: {', '.join([f'{k}: {v}' for k, v in all_model_counts.items()])}")
    print(f"Model %: {', '.join([f'{k}: {v / num_requests * 100:.3f}%' for k, v in all_model_counts.items()])}")

    # Write results
    with open(args.result_file, "a") as fout:
        value = {
            "task": "mmlu",
            "backend": args.backend,
            "num_gpus": 1,
            "latency": round(total_latency, 3),
            "accuracy": round(weighted_acc, 3),
            "num_requests": num_requests,
            "model_counts": all_model_counts,
            "other": {
                "nsub": args.nsub,
                "parallel": args.parallel,
            }
        }
        fout.write(json.dumps(value) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ntrain", "-k", type=int, default=5)
    parser.add_argument("--data_dir", "-d", type=str, default="data")
    parser.add_argument("--save_dir", "-s", type=str, default="results")
    parser.add_argument("--nsub", type=int, default=60)
    args = add_common_sglang_args_and_parse(parser)
    main(args)
