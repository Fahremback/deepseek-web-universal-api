# DeepSeek Web API client

![Tests](https://github.com/Fahremback/deepseek-web-universal-api/actions/workflows/tests.yml/badge.svg)

**Local OpenAI- and Anthropic-compatible HTTP gateway for DeepSeek Web**, with
SSE streaming, agent tool calls, chat history, media uploads and a direct Python
client. Requires an authenticated DeepSeek Web account; this is an unofficial
client, not DeepSeek's commercial API.

## Quick start

**Codex com DeepSeek no Windows (já configurado):** abra o arquivo
`Abrir Codex DeepSeek.cmd` com dois cliques. Ele inicia o Codex CLI usando
o DeepSeek Web e encerra o gateway local ao sair. Para um comando direto,
use `python run_codex.py`. Não é necessário alterar a configuração global do
Codex. É necessário manter a sessão do DeepSeek válida.

**API para qualquer harness compatível com OpenAI/Anthropic no Windows:**
abra `Abrir API DeepSeek.cmd` com dois cliques e mantenha a janela aberta.
Configure o programa cliente com a base URL OpenAI
`http://127.0.0.1:8000/v1` (ou Anthropic `http://127.0.0.1:8000`) e o
modelo `deepseek-web`. Os dois atalhos usam a mesma sessão DeepSeek salva,
mas executam servidores locais independentes. Se um aplicativo exigir um campo
de chave API, poderá utilizar um marcador local; por padrão, a API se limita
à interface de loopback e **não exige autenticação local**. Para protegê-la
com uma chave, defina `DEEPSEEK_BRIDGE_API_KEY` antes de iniciá-la e configure
a mesma chave no cliente. Não exponha a porta 8000 à rede pública.

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

Para utilizar o **Codex CLI** com o gateway apenas durante a sessão, sem
modificar `~/.codex/config.toml` e sem instalar um serviço persistente:

```powershell
python run_codex.py
python run_codex.py -- exec "Analise este projeto"
```

O iniciador cria uma chave de API local temporária, escolhe uma porta livre,
inicia o gateway em `127.0.0.1`, configura o modelo `deepseek-web` e o protocolo
OpenAI Responses para o processo Codex e encerra o gateway quando o Codex sai.
É necessário ter o Codex CLI instalado e uma sessão DeepSeek Web autenticada.
Isso **não** reconfigura o aplicativo Codex Desktop nem mantém um servidor 24/7.
Veja também [a integração e seus limites](docs/HARNESSES.md).

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

**Recommended: sign in on DeepSeek's real website.** Run:

```powershell
python api_client.py --login-web
```

The command first checks for an already authenticated session in **Perplexity
Comet**, including its secondary profiles. If verified, it imports that session
without opening a window. Otherwise, it opens **https://chat.deepseek.com/sign_in**
in the **system default browser**. Log in there normally, including any
interactive challenge. It validates the token with the same headers as the
gateway, saves it to the available OS keyring, and reports progress while
waiting. Offline profile import supports Chromium-based browsers (Comet, Edge,
Chrome, Brave, Chromium, Vivaldi) on Windows, macOS and Linux when their
profile format and the local reader are supported.
The reader does not collect account passwords.

For an optional dedicated browser flow, use `--login-browser msedge`,
`--login-browser chromium`, `--login-browser firefox` or `--login-browser webkit`.
The matching Playwright browser binary must be installed first (`python -m playwright install chromium firefox webkit`).
Each managed browser keeps a persistent profile outside the project folder,
under the OS user data directory (on Windows, `%LOCALAPPDATA%`; on macOS,
`~/Library/Application Support`; on Linux, `$XDG_DATA_HOME` or `~/.local/share`).
The dedicated browser closes after a successful login. The ordinary default
browser can be opened on any system, but importing an existing session from
Firefox, Safari or other unrecognized profile formats is not automatic.

If the site invalidates the login later and no account-password login was
explicitly provisioned, use `--login-web` again to sign in interactively. This
is not an unattended password login or a way around additional verification.
On authentication failure, the running HTTP client first checks the credential
vault, then any configured account login, then tries to import a refreshed
Comet session before returning an authentication error.

For a computer with an existing authenticated DeepSeek session in a supported
Chromium-based browser, import it once from local storage using HTTP validation; no browser
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

Normal API requests neither open nor install a browser. For unattended renewal when
the session token expires, provision the account login once. The password is
prompted without appearing in command history, then stored in a **separate**
Windows Credential Manager (OS keyring) entry alongside the session entry:

```powershell
python api_client.py --login --email "your@email.com"
python api_client.py --json "Responda apenas OK."
```

Mobile login is also supported with `--login --mobile NUMBER --area-code CODE`.
When running without an interactive terminal, explicitly pass `--password-stdin`
to read the password from standard input; never pass the password as a CLI
argument. An expired session triggers a single HTTP login at
`POST https://chat.deepseek.com/api/v0/users/login`, saves the new token in
the system vault, and retries the failed request at most once. There is no
separate refresh-token endpoint. This **unofficial private API** can change,
reject credentials, or require additional challenges; unattended login is not
guaranteed in those cases. The client will report an authentication error.

```powershell
python -m pip install -r requirements.txt
$env:DEEPSEEK_TOKEN = "YOUR_TOKEN_ON_FIRST_RUN"
python api_client.py "Responda apenas OK."
Remove-Item Env:DEEPSEEK_TOKEN
python api_client.py --json "Responda apenas OK."
```

The import helper uses optional offline readers under `deepseek_api/tools/`.
They are not needed by the HTTP API runtime. No plaintext token file is made.

To remove the saved token **and saved account login**, run
`python api_client.py --forget-credentials`.

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
