from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Literal, Sequence, Type, TypeVar, Any
import time

from langchain_core.runnables import RunnableConfig
# LangChain OpenAI integration
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
# LangChain core message types
from langchain_core.messages import SystemMessage, HumanMessage
from pydantic import BaseModel, SecretStr

T = TypeVar("T", bound=BaseModel)

@dataclass
class ChatLLMClient:
    """
    A simple LLM wrapper class:
    - Expose only invoke(prompt)->str to modules
    - Use LangChain 1.0 ChatOpenAI internally
    Keep upper-level modules independent of the specific model SDK / LangChain details, making model swaps easier later.
    """
    provider: str
    model: str
    temperature: float = 0.2
    timeout_s: int = 60
    api_key: str = ""
    base_url: Optional[str] = None  # optional if you use a proxy or private gateway
    enable_thinking: bool = False  # added

    def __post_init__(self) -> None:
        """
        Create the underlying ChatOpenAI after dataclass initialization.
        LangChain reads the key from parameters or environment variables; pass it explicitly to keep control.
        """
        # ChatOpenAI is a Runnable; it supports invoke() and returns AIMessage
        # Documentation reference: LangChain ChatOpenAI integration guide and reference docs :contentReference[oaicite:2]{index=2}

        self._chat = ChatOpenAI(
            model=self.model,
            temperature=self.temperature,
            timeout=self.timeout_s,
            api_key=SecretStr(self.api_key),
            base_url=self.base_url,
            extra_body={
                "enable_thinking": self.enable_thinking,
                "chat_template_kwargs": {# option compatible with disabling thinking in vLLM
                    "enable_thinking": self.enable_thinking
                }
            }
        )

    def invoke(self, user_prompt: str, *, system_prompt: Optional[str] = None) -> str:
        """
        Minimal invocation method:
        - prompt: user content (HumanMessage)
        - system: optional system prompt (SystemMessage)
        Returns:
        - model-generated text content (str)
        """
        messages = []
        if system_prompt:
            messages.append(SystemMessage(content=system_prompt))
        messages.append(HumanMessage(content=user_prompt))

        # ChatOpenAI.invoke(messages) -> AIMessage
        resp = self._chat.invoke(messages)
        return resp.content


    def invoke_batch(
            self,
            user_prompts: Sequence[str],
            *,
            system_prompt: Optional[str] = None,
            max_concurrency: int = 5,
    ) -> List[str]:
        """
        Batch invocation with concurrency control.
        """
        inputs = []
        for up in user_prompts:
            msgs = []
            if system_prompt:
                msgs.append(SystemMessage(content=system_prompt))
            msgs.append(HumanMessage(content=up))
            inputs.append(msgs)

        # batch: concurrent calls (default thread-pool parallelism); max_concurrency controls concurrency
        config: RunnableConfig = {"max_concurrency": max_concurrency}
        results = self._chat.batch(inputs, config=config)
        return [r.content for r in results]

    # ============================
    # [ADD] Structured output (Pydantic)
    # ============================
    def invoke_structured(
            self,
            user_prompt: str,
            *,
            schema: Type[T],
            system_prompt: Optional[str] = None,
            # The following two parameters leave room for different LangChain versions / underlying providers
            method: Optional[str] = None,
            strict: Optional[bool] = None,
    ) -> T:
        """
        Structured output: return a Pydantic model instance for schema.

        - schema: Pydantic BaseModel subclass
        - method/strict: optional parameters for compatibility with different LangChain versions (not always used)
        """
        messages = []
        if system_prompt:
            messages.append(SystemMessage(content=system_prompt))
        messages.append(HumanMessage(content=user_prompt))

        # 1) Build the structured runnable
        # Different versions of with_structured_output support slightly different parameters, so keep dependencies minimal here:
        kwargs: dict[str, Any] = {}
        if method is not None:
            kwargs["method"] = method
        if strict is not None:
            kwargs["strict"] = strict

        structured = self._chat.with_structured_output(schema, **kwargs)

        # 2) Invoke
        resp = structured.invoke(messages)

        # 3) Compatibility: it may return a schema instance directly, or a dict
        if isinstance(resp, schema):
            return resp
        if isinstance(resp, dict):
            return schema.model_validate(resp)

        # In rare cases it may return another type (such as a string); raise early to expose the issue sooner
        raise TypeError(f"Structured output type unexpected: {type(resp)}")

    def invoke_structured_batch(
            self,
            user_prompts: Sequence[str],
            *,
            schema: Type[T],
            system_prompt: Optional[str] = None,
            max_concurrency: int = 5,
            method: Optional[str] = None,
            strict: Optional[bool] = None,
    ) -> List[T]:
        """
        Batch structured output: return a list of Pydantic model instances.

        Note: if the model occasionally outputs invalid content, this raises an exception (better for a reliable evaluation pipeline).
        If you want non-interrupting degradation, an allow_fallback=True strategy can be added.
        """
        inputs = []
        for up in user_prompts:
            msgs = []
            if system_prompt:
                msgs.append(SystemMessage(content=system_prompt))
            msgs.append(HumanMessage(content=up))
            inputs.append(msgs)

        kwargs: dict[str, Any] = {}
        if method is not None:
            kwargs["method"] = method
        if strict is not None:
            kwargs["strict"] = strict

        structured = self._chat.with_structured_output(schema, **kwargs)

        config: RunnableConfig = {"max_concurrency": max_concurrency}
        results = structured.batch(inputs, config=config)

        out: List[T] = []
        for r in results:
            if isinstance(r, schema):
                out.append(r)
            elif isinstance(r, dict):
                out.append(schema.model_validate(r))
            else:
                raise TypeError(f"Structured output type unexpected: {type(r)}")
        return out


@dataclass
class EmbeddingClient:
    """
    Embedding model wrapper class (for RAG / vector retrieval)

    Public API:
    - embed_documents(texts) -> List[List[float]]
    - embed_query(text) -> List[float]
    """
    provider: str
    model: str
    api_key: str = ""
    base_url: Optional[str] = None
    timeout_s: int = 60
    dimensions: Optional[int] = None
    encoding_format: Literal["float", "base64"] = "float"  # openai-compatible commonly used

    def __post_init__(self) -> None:
        provider = (self.provider or "").lower()
        if provider == "openai":
            self._emb = OpenAIEmbeddings(
                model=self.model,
                dimensions=self.dimensions,
                api_key=SecretStr(self.api_key),
                base_url=self.base_url,
                timeout=self.timeout_s,
            )
            self._mode = "openai_langchain"
            return
        if provider == "openai_compatible":
            from openai import OpenAI
            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout_s,
            )
            self._mode = "openai_sdk"
            return
        raise ValueError(
            f"Unsupported embedding provider: {self.provider}. "
            f"Use 'openai' or 'openai_compatible'."
        )

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """
        Batch embedding (for ingestion)
        """
        if not texts:
            return []

        if self._mode == "openai_langchain":
            return self._emb.embed_documents(texts)

            # openai-compatible
        resp = self._client.embeddings.create(
            model=self.model,
            input=texts,
            dimensions=self.dimensions,
            encoding_format=self.encoding_format,
        )
        return [d.embedding for d in resp.data]

    def embed_query(self, text: str) -> List[float]:
        """
        Single embedding (for retrieval queries)
        """
        if not text:
            return []

        if self._mode == "openai_langchain":
            return self._emb.embed_query(text)

        resp = self._client.embeddings.create(
            model=self.model,
            input=[text],
            dimensions=self.dimensions,
            encoding_format=self.encoding_format,
        )
        return resp.data[0].embedding
