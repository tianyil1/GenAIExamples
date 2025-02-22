#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (c) 2024 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import requests
import json
import types
from concurrent import futures
from typing import Optional
from fastapi import FastAPI, APIRouter
from fastapi.responses import RedirectResponse, StreamingResponse
from langchain.callbacks.streaming_stdout import StreamingStdOutCallbackHandler
from langchain_community.llms import HuggingFaceEndpoint
from langchain_core.pydantic_v1 import BaseModel
from starlette.middleware.cors import CORSMiddleware
from openai_protocol import ChatCompletionRequest, ChatCompletionResponse

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"])

class CodeGenAPIRouter(APIRouter):
    def __init__(self, entrypoint) -> None:
        super().__init__()
        self.entrypoint = entrypoint
        print(f"[codegen - router] Initializing API Router, entrypoint={entrypoint}")

        # Define LLM
        self.llm = HuggingFaceEndpoint(
            endpoint_url=entrypoint,
            max_new_tokens=512,
            top_k=10,
            top_p=0.95,
            typical_p=0.95,
            temperature=0.01,
            repetition_penalty=1.03,
            streaming=True,
        )
        print("[codegen - router] LLM initialized.")

    def is_generator(self, obj):
        return isinstance(obj, types.GeneratorType)

    def handle_chat_completion_request(self, request: ChatCompletionRequest):
        try:
            print(f"Predicting chat completion using prompt '{request.prompt}'")
            buffered_texts = ""
            if request.stream:
                generator = self.llm(request.prompt, callbacks=[StreamingStdOutCallbackHandler()])
                if not self.is_generator(generator):
                    generator = (generator,)
                def stream_generator():
                    nonlocal buffered_texts
                    for output in generator:
                        yield f"data: {output}\n\n"
                    yield f"data: [DONE]\n\n"
                return StreamingResponse(stream_generator(), media_type="text/event-stream")
            else:
                response = self.llm(request.prompt)
        except Exception as e:
            print(f"An error occurred: {e}")
        else:
            print("Chat completion finished.")
            return ChatCompletionResponse(response=response)

tgi_endpoint = os.getenv("TGI_ENDPOINT", "http://localhost:8080")
router = CodeGenAPIRouter(tgi_endpoint)

app.include_router(router)

def check_completion_request(request: BaseModel) -> Optional[str]:
    if request.temperature is not None and request.temperature < 0:
        return f"Param Error: {request.temperature} is less than the minimum of 0 --- 'temperature'"

    if request.temperature is not None and request.temperature > 2:
        return f"Param Error: {request.temperature} is greater than the maximum of 2 --- 'temperature'"

    if request.top_p is not None and request.top_p < 0:
        return f"Param Error: {request.top_p} is less than the minimum of 0 --- 'top_p'"

    if request.top_p is not None and request.top_p > 1:
        return f"Param Error: {request.top_p} is greater than the maximum of 1 --- 'top_p'"

    if request.top_k is not None and (not isinstance(request.top_k, int)):
        return f"Param Error: {request.top_k} is not valid under any of the given schemas --- 'top_k'"

    if request.top_k is not None and request.top_k < 1:
        return f"Param Error: {request.top_k} is greater than the minimum of 1 --- 'top_k'"

    if request.max_new_tokens is not None and (not isinstance(request.max_new_tokens, int)):
        return f"Param Error: {request.max_new_tokens} is not valid under any of the given schemas --- 'max_new_tokens'"

    return None

def filter_code_format(code):
    language_prefixes = {
        "go": "```go",
        "c": "```c",
        "cpp": "```cpp",
        "java": "```java",
        "python": "```python",
        "typescript": "```typescript"
    }
    suffix = "\n```"

    # Find the first occurrence of a language prefix
    first_prefix_pos = len(code)
    for prefix in language_prefixes.values():
        pos = code.find(prefix)
        if pos != -1 and pos < first_prefix_pos:
            first_prefix_pos = pos + len(prefix) + 1

    # Find the first occurrence of the suffix after the first language prefix
    first_suffix_pos = code.find(suffix, first_prefix_pos + 1)

    # Extract the code block
    if first_prefix_pos != -1 and first_suffix_pos != -1:
        return code[first_prefix_pos:first_suffix_pos]
    elif first_prefix_pos != -1:
        return code[first_prefix_pos:]

    return code

# router /v1/code_generation only supports non-streaming mode.
@router.post("/v1/code_generation")
async def code_generation_endpoint(chat_request: ChatCompletionRequest):
    if router.use_deepspeed:
        responses = []

        def send_request(port):
            try:
                url = f'http://{router.host}:{port}/v1/code_generation'
                response = requests.post(url, json=chat_request.dict())
                response.raise_for_status()
                json_response = json.loads(response.content)
                cleaned_code = filter_code_format(json_response['response'])
                chat_completion_response = ChatCompletionResponse(response=cleaned_code)
                responses.append(chat_completion_response)
            except requests.exceptions.RequestException as e:
                print(f"Error sending/receiving on port {port}: {e}")

        with futures.ThreadPoolExecutor(max_workers=router.world_size) as executor:
            worker_ports = [router.port + i + 1 for i in range(router.world_size)]
            executor.map(send_request, worker_ports)
        if responses:
            return responses[0]
    else:
        ret = check_completion_request(chat_request)
        if ret is not None:
            raise RuntimeError("Invalid parameter.")
        return router.handle_chat_completion_request(chat_request)

# router /v1/code_chat supports both non-streaming and streaming mode.
@router.post("/v1/code_chat")
async def code_chat_endpoint(chat_request: ChatCompletionRequest):
    if router.use_deepspeed:
        if chat_request.stream:
            responses = []
            def generate_stream(port):
                url = f'http://{router.host}:{port}/v1/code_generation'
                response = requests.post(url, json=chat_request.dict(), stream=True, timeout=1000)
                responses.append(response)
            with futures.ThreadPoolExecutor(max_workers=router.world_size) as executor:
                worker_ports = [router.port + i + 1 for i in range(router.world_size)]
                executor.map(generate_stream, worker_ports)

            while not responses:
                pass
            def generate():
                if responses[0]:
                    for chunk in responses[0].iter_lines(decode_unicode=False, delimiter=b"\0"):
                        if chunk:
                            yield f"data: {chunk}\n\n"
                    yield f"data: [DONE]\n\n"

            return StreamingResponse(generate(), media_type="text/event-stream")
        else:
            responses = []

            def send_request(port):
                try:
                    url = f'http://{router.host}:{port}/v1/code_generation'
                    response = requests.post(url, json=chat_request.dict())
                    response.raise_for_status()
                    json_response = json.loads(response.content)
                    chat_completion_response = ChatCompletionResponse(response=json_response['response'])
                    responses.append(chat_completion_response)
                except requests.exceptions.RequestException as e:
                    print(f"Error sending/receiving on port {port}: {e}")

            with futures.ThreadPoolExecutor(max_workers=router.world_size) as executor:
                worker_ports = [router.port + i + 1 for i in range(router.world_size)]
                executor.map(send_request, worker_ports)
            if responses:
                return responses[0]
    else:
        ret = check_completion_request(chat_request)
        if ret is not None:
            raise RuntimeError("Invalid parameter.")
        return router.handle_chat_completion_request(chat_request)

@app.get("/")
async def redirect_root_to_docs():
    return RedirectResponse("/docs")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)

