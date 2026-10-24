# DeepSeek Web API client

![Tests](https://github.com/Fahremback/deepseek-web-universal-api/actions/workflows/tests.yml/badge.svg)

**Local OpenAI- and Anthropic-compatible HTTP gateway for DeepSeek Web**, with
SSE streaming, agent tool calls, chat history, media uploads and a direct Python
client. Requires an authenticated DeepSeek Web account; this is an unofficial
client, not DeepSeek's commercial API.

## Quick start

```powershell
git clone https://github.com/Fahremback/deepseek-web-universal-api.git
cd deepseek-web-universal-api
python -m pip install -r requirements.txt
python openai_server.py
```

OpenAI-compatible base URL: `http://127.0.0.1:8000/v1` with model
`deepseek-web`. Anthropic-compatible base URL: `http://127.0.0.1:8000`.
See [authentication setup](docs/INSTALACAO.md) and
[harness integration](docs/HARNESSES.md). The gateway is bound to localhost by
default. Tool calling is emulated through structured prompts and validation.

## Documentacao e paper

- [Indice tecnico completo](docs/README.md)
- [API universal para harnesses OpenAI/Anthropic](docs/HARNESSES.md)
- [Artigo tecnico (Markdown)](paper/PAPER.md)
- [Artigo tecnico (PDF)](paper/PAPER.pdf)
- [Exemplos Python](examples/)

O cliente implementa um protocolo HTTP privado do DeepSeek Web; nao e a API publica do provedor e nao inicia navegador.

## Servidor OpenAI / Anthropic para harnesses

```powershell
python openai_server.py --host 127.0.0.1 --port 8000
```

Base URL OpenAI-compatible: `http://127.0.0.1:8000/v1`, modelo `deepseek-web` (ou `deepseek-web-reasoner`). Para clientes Anthropic Messages, use `http://127.0.0.1:8000`. Suporta `/v1/chat/completions`, `/v1/responses`, `/v1/messages`, SSE, ferramentas e anexos em base64. A tradução de ferramentas e papéis utiliza prompting validado, já que o protocolo privado web não expõe essas estruturas nativamente no cliente.

Configuração e exemplos: [HARNESSES.md](docs/HARNESSES.md).

## Anexos: imagens e documentos

```powershell
python -u api_client.py --stream --file "imagem.png" --file "documento.pdf" "Analise os anexos."
```

O cliente executa upload multipart por HTTP, resolve o PoW do upload, aguarda a leitura de cada arquivo e inclui os IDs em `ref_file_ids` na conversa. Foram validados remotamente PNG e PDF. Veja [mídias e arquivos](docs/MIDIAS.md).

The Python client reproduces the DeepSeek Web session, PoW and streaming
protocol captured by Reverse API Engineer. It performs requests directly via
HTTP and requires no browser at runtime.

## Authentication

For a computer with an existing authenticated DeepSeek session in Edge or
Chrome, import it once from local storage using HTTP validation; no browser
process is started. The session is saved in the operating system's credential
vault (Windows Credential Manager, macOS Keychain, or supported Linux keyring).
Later HTTP API calls retrieve it automatically.

```powershell
python import_existing_session.py
python api_client.py --json "Responda apenas OK."
```

Alternatively, the account owner can provide a bearer token once through
`DEEPSEEK_TOKEN`, `DEEPSEEK_TOKEN_FILE`, or `--token`. It is persisted after
the first successful authenticated API call.

No browser is opened or installed by the client. If the site invalidates the
token, a new account credential is needed; the DeepSeek Web private API does
not offer this client a verified unattended sign-in flow.

```powershell
python -m pip install -r requirements.txt
$env:DEEPSEEK_TOKEN = "YOUR_TOKEN_ON_FIRST_RUN"
python api_client.py "Responda apenas OK."
Remove-Item Env:DEEPSEEK_TOKEN
python api_client.py --json "Responda apenas OK."
```

The import helper uses optional offline readers under `deepseek_api/tools/`.
They are not needed by the HTTP API runtime. No plaintext token file is made.

To remove the saved credential, run `python api_client.py --forget-credentials`.

## Check

```powershell
python api_client.py --selftest
```

## Real-time streaming and thinking

Output response tokens immediately as plain text (the default):

```powershell
python -u api_client.py "Explique gravidade."
```

To consume real-time events from other programs, stream newline-delimited JSON:

```powershell
python -u api_client.py --stream --thinking "Explique gravidade."
```

Each line is flushed immediately. `type: "thinking"` provides reasoning deltas,
`type: "text"` provides answer deltas, `type: "title"` updates the chat title,
and the last event is `type: "done"` with session and message IDs.
`--thinking` enables reasoning on DeepSeek; omit it for normal chat.
If the DeepSeek server finishes a chunk with `status: INCOMPLETE` (output token
limit), the client automatically sends `POST /api/v0/chat/continue` for the same
message and keeps streaming only new text. JSONL emits `type: "continue"` with
the attempt number. The final result contains `continuations`; `type: "done"`
is emitted only once after the full answer finishes. Use `--no-auto-continue`
to disable this behavior or `--max-continues N` to change the safety cap (20).
Use `--json` without `--stream` when a single complete JSON result is required.
For Python callers, `DeepSeekClient.stream_completion(...)` yields `StreamChunk`
objects, while `DeepSeekClient.chat(...)` accepts `on_chunk`, `on_text`, and
`on_thinking` callbacks that execute as events arrive.

The API endpoint is private and may change when DeepSeek updates its website.
