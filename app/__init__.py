# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Monkeypatch Gemini to automatically retry on 429 / RESOURCE_EXHAUSTED errors
import asyncio
import logging
import re
from google.adk.models.google_llm import Gemini

original_generate_content_async = Gemini.generate_content_async

async def patched_generate_content_async(self, llm_request, stream=False):
    max_retries = 5
    delay = 5.0
    for attempt in range(max_retries):
        try:
            async for response in original_generate_content_async(self, llm_request, stream):
                yield response
            return
        except Exception as e:
            err_msg = str(e).lower()
            if any(kwd in err_msg for kwd in ["quota", "exhausted", "429", "503", "rate limit", "resource_exhausted", "unavailable", "high demand"]):
                if attempt == max_retries - 1:
                    logging.error(f"[PATCH] Rate limit/Temporary error retries exhausted: {e}")
                    raise
                match = re.search(r"retry in ([\d\.]+)s", str(e), re.IGNORECASE)
                if not match:
                    match = re.search(r"retry after ([\d\.]+)s", str(e), re.IGNORECASE)
                if not match:
                    match = re.search(r"retry delay':\s*'([\d\.]+)s'", str(e), re.IGNORECASE)
                
                wait_time = float(match.group(1)) + 1.0 if match else delay
                logging.warning(
                    f"[PATCH] Gemini API error hit. Waiting {wait_time:.2f}s before retrying "
                    f"(attempt {attempt + 1}/{max_retries})..."
                )
                print(f"[RETRY] Gemini API error hit. Waiting {wait_time:.2f}s before retrying...")
                await asyncio.sleep(wait_time)
                delay *= 2
            else:
                raise

Gemini.generate_content_async = patched_generate_content_async

from .agent import app

__all__ = ["app"]
