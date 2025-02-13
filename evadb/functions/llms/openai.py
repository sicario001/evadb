# coding=utf-8
# Copyright 2018-2023 EvaDB
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


import os
from typing import List

from retry import retry

from evadb.functions.llms.base import BaseLLM
from evadb.utils.generic_utils import (
    try_to_import_openai,
    try_to_import_tiktoken,
    validate_kwargs,
)

MODEL_CHOICES = [
    # "gpt-4-32k",
    # "gpt-4-32k-0314",
    "gpt-4",    
    "gpt-4-0314",
    "gpt-3.5-turbo-16k",
    "gpt-3.5-turbo-16k-0613",
    "gpt-3.5-turbo",
    "gpt-3.5-turbo-0301",
]

_DEFAULT_PARAMS = {
    "model": "gpt-3.5-turbo",
    "temperature": 0.0,
    "max_tokens": 1000,
    "messages": [],
}

DEFAULT_PROMPT = "You are a helpful assistant that accomplishes user tasks."

class OpenAILLM(BaseLLM):
    @property
    def name(self) -> str:
        return "OpenAILLM"

    def setup(
        self,
        openai_api_key="",
        **kwargs,
    ) -> None:
        super().setup(**kwargs)

        try_to_import_openai()
        from openai import OpenAI

        api_key = openai_api_key
        if len(openai_api_key) == 0:
            api_key = os.environ.get("OPENAI_API_KEY", "")
        assert (
            len(api_key) != 0
        ), "Please set your OpenAI API key using SET OPENAI_API_KEY = 'sk-' or environment variable (OPENAI_API_KEY)"

        self.client = OpenAI(api_key=api_key)

        validate_kwargs(kwargs, allowed_keys=_DEFAULT_PARAMS.keys(), required_keys=[])
        self.model_params = {**_DEFAULT_PARAMS, **kwargs}

    def model_selection(self, idx: int, prompt: str, queries: str, contents: str, cost: float, budget: float):
        query = queries[idx]
        content = contents[idx]

        for model in MODEL_CHOICES:
            self.model_params["model"] = model
            _, dollar_cost = self.get_max_cost(prompt, query, content)
            for i in range(idx+1, len(queries)):
                self.model_params["model"] = MODEL_CHOICES[-1]
                dollar_cost += self.get_max_cost(None, queries[i], contents[i])[1]
            if budget - dollar_cost >= 0:
                return model
      
        return MODEL_CHOICES[-1]

    def generate(self, queries: List[str], contents: List[str], prompt: str) -> List[str]:
        budget = int(os.environ.get("OPENAI_BUDGET", None))
        cost = 0

        @retry(tries=6, delay=20)
        def completion_with_backoff(**kwargs):
            return self.client.chat.completions.create(**kwargs)
        
        results = []
        models = []

        for query, content, idx in zip(queries, contents, range(len(queries))):
            def_sys_prompt_message = {
                "role": "system",
                "content": prompt
                if prompt is not None
                else DEFAULT_PROMPT,
            }

            # select model
            model = self.model_selection(idx, prompt, queries, contents, cost, budget)
            assert model is not None, "OpenAI budget exceeded!"

            models.append(model)
            self.model_params["model"] = model

            self.model_params["messages"].append(def_sys_prompt_message)
            self.model_params["messages"].extend(
                [
                    {
                        "role": "user",
                        "content": f"Here is some context : {content}",
                    },
                    {
                        "role": "user",
                        "content": f"Complete the following task: {query}",
                    },
                ],
            )

            response = completion_with_backoff(**self.model_params)
            answer = response.choices[0].message.content

            cost_query = self.get_cost(prompt, query, content, answer)[1]

            cost += cost_query
            budget -= cost_query

            results.append(answer)

        return results, models

    def get_cost(self, prompt: str, query: str, content: str, response: str):
        try_to_import_tiktoken()
        import tiktoken

        encoding = tiktoken.encoding_for_model(self.model_params["model"])
        # print(type(prompt), type(query), type(content), type(response))
        num_prompt_tokens = len(encoding.encode(DEFAULT_PROMPT))
        if prompt is not None:
            num_prompt_tokens = len(encoding.encode(prompt))
        num_query_tokens = len(encoding.encode(query))
        num_content_tokens = len(encoding.encode(content))
        num_response_tokens = self.model_params["max_tokens"]
        if response is not None:
            num_response_tokens = len(encoding.encode(response))

        model_stats = self.get_model_stats(self.model_params["model"])

        token_consumed = (num_prompt_tokens+num_query_tokens+num_content_tokens, num_response_tokens)
        dollar_cost = (
            model_stats["input_cost_per_token"] * token_consumed[0]
            + model_stats["output_cost_per_token"] * token_consumed[1]
        )
        return token_consumed, dollar_cost
    
    def get_max_cost(self, prompt: str, query: str, content: str):
        return self.get_cost(prompt, query, content, response=None)
