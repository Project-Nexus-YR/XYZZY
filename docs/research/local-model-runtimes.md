# Local-model support for XYZZY — research notes

## Source read first
- `C:\Users\OME\Yashiru\Personal\Main\XYZZY\src\multiplayer\model_providers\openai_responses.py`
  - `OpenAIResponsesProvider` posts to a hardcoded `_RESPONSES_URL = "https://api.openai.com/v1/responses"` (line 13), no base-URL override today.
  - Uses Responses API `text.format = {type: json_schema, name, strict, schema}` for structured output (lines 118-146) — two modes: strict (synthesis, closed schema with "claims") and non-strict (step schema with "action" enum).
  - Config today: `OPENAI_API_KEY`, `XYZZY_OPENAI_MODEL`, `XYZZY_MODEL_TIMEOUT_SECONDS` (lines 292-301), all read in `model_provider_from_environment`.
  - `WorkflowOnlyModelProvider` is the credential-free fallback when no key is set.
  - Decoding depends on Responses-specific response shape (`output_text` / `output[].content[].type=="output_text"`, `payload["id"]`) — see `_extract_output_text` (lines 263-284) and `_decode_response` (193-228).

## 1. Local runtime API surfaces (as of 2026-08)

- **Ollama** — exposes both `/v1/chat/completions` (long-standing) AND `/v1/responses` since v0.13.3, but the Responses support is explicitly **non-stateful** (no `previous_response_id`/conversation continuation). Structured output/JSON schema is supported via the `format` field (schema-constrained JSON, "Structured Outputs" since v0.3.0) — this is documented for the native Ollama API and chat completions; not clearly documented for `/v1/responses` json_schema format parity with OpenAI's `text.format`.
  - https://docs.ollama.com/api/openai-compatibility
  - https://ollama.com/blog/openai-compatibility
  - https://docs.ollama.com/capabilities/structured-outputs
  - https://ollama.com/blog/structured-outputs

- **LM Studio** — OpenAI-compatible surface is `/v1/chat/completions` only (no `/v1/responses` found in docs). Structured output supported via `response_format.json_schema` on chat completions, matching OpenAI's original Structured Output API shape. GGUF models use llama.cpp grammar sampling; MLX models use Outlines.
  - https://lmstudio.ai/docs/developer/openai-compat/structured-output

- **vLLM** — has its own **Responses API** at `/v1/responses` (plus `/v1/responses/{id}`, `/v1/responses/{id}/cancel}`), for text-generation models only, alongside `/v1/chat/completions`. Structured output is supported via `guided_json`/`response_format` + backends (outlines, lm-format-enforcer, xgrammar) on both Completions and Chat APIs.
  - https://docs.vllm.ai/en/latest/serving/online_serving/openai_compatible_server/
  - https://docs.vllm.ai/en/latest/features/structured_outputs/

- **llama.cpp server** — OpenAI-compatible `/v1/chat/completions` only (no `/v1/responses`). `response_format` supports `json_object` and `json_schema`/schema-constrained JSON, but there are open GitHub issues reporting `json_schema` under `response_format` failing on `/v1/chat/completions` in some versions/configs (grammar conflict errors).
  - https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md
  - https://github.com/ggml-org/llama.cpp/issues/11988
  - https://github.com/ggml-org/llama.cpp/issues/11847

### Summary table
| Runtime | `/v1/responses` | `/v1/chat/completions` | JSON-schema structured output |
|---|---|---|---|
| Ollama | yes (non-stateful, since v0.13.3) | yes | yes (via `format`, chat/native path; parity on `/v1/responses` unclear) |
| LM Studio | no | yes | yes (`response_format.json_schema`) |
| vLLM | yes | yes | yes (`guided_json`/`response_format`, multiple backends) |
| llama.cpp server | no | yes | yes, but flaky per open issues |

## 2. Base-URL env var vs new provider class

Only **Ollama and vLLM** expose `/v1/responses` at all, and even Ollama's is a reduced/non-stateful surface. **LM Studio and llama.cpp server have no `/v1/responses` endpoint whatsoever** — pointing `OpenAIResponsesProvider` at them via a `XYZZY_OPENAI_BASE_URL` override would 404, because the provider's request/response shape is Responses-specific (`input`/`text.format` request, `output_text`/`output[].content` response, `payload["id"]`), not the Chat Completions request/response shape (`messages`, `response_format`, `choices[0].message.content`).

- A base-URL-only change on the existing `OpenAIResponsesProvider` covers **at most Ollama and vLLM**, and even for Ollama the Responses support is new/reduced, so behavior there is less proven than its chat-completions path.
- A **new Chat-Completions provider class** (mirroring `OpenAIResponsesProvider`'s structure/error handling but hitting `{base_url}/v1/chat/completions` with `messages` + `response_format.json_schema`) covers **all four runtimes** (Ollama, LM Studio, vLLM, llama.cpp server) since chat completions is the universal common denominator.
- Cheapest-but-correct: add **one new provider class** for Chat Completions, reusing the existing schema/step-decoding logic, and gate it with a base-URL env var (e.g. `XYZZY_OPENAI_BASE_URL` or a distinct `XYZZY_LOCAL_MODEL_BASE_URL`) plus an existing-shaped `XYZZY_OPENAI_MODEL`. Do NOT try to make the Responses provider "local" via base-URL alone — it silently drops LM Studio and llama.cpp, and even Ollama/vLLM Responses parity isn't fully proven.

## 3. Comparable OSS projects — config shape

- **LibreChat** — `librechat.yaml`, `endpoints.custom[]` list, each entry: `name`, `apiKey` (placeholder or `${ENV_VAR}`), `baseURL` (e.g. `http://host.docker.internal:11434/v1/` for Ollama, `http://127.0.0.1:8023/v1` for vLLM), `models`. One config shape covers any OpenAI-compatible chat-completions backend.
  - https://www.librechat.ai/docs/quick_start/custom_endpoints
  - https://www.librechat.ai/docs/configuration/librechat_yaml/ai_endpoints/vllm

- **Open WebUI** — plain env vars: `OLLAMA_BASE_URL` (Ollama-native path) and separately `OPENAI_API_BASE_URL` + `OPENAI_API_KEY` for any OpenAI-compatible server (vLLM, LocalAI, etc.), settable via env or Admin UI (UI value takes precedence once persisted).
  - https://docs.openwebui.com/reference/env-configuration/

Both examples use a single **base-URL + api-key pair** against the **Chat Completions** shape (not Responses) to reach the widest set of local runtimes.

## Not determined
- Whether Ollama's `/v1/responses` supports the same `text.format.json_schema` strict-mode shape XYZZY relies on for its "claims" schema — docs found describe schema-constrained `format` on the native/chat path, not confirmed for `/v1/responses` parity.
- Did not inspect other XYZZY files (e.g. provider registry/factory, tests) beyond the one named entry point — scope was limited to the file given.
