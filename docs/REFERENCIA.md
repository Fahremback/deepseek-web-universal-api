# Referência de API Python e CLI

## Construtor

```python
DeepSeekClient(
    token=None, device_id=None, *,
    base_url="https://chat.deepseek.com",
    locale=None, timezone_offset=None,
    cookies=None, hif_leim=None,
    timeout=30.0, stream_timeout=180.0, verbose=False
)
```

Usa `requests.Session` e credencial explícita, ambiente/arquivo ou cofre. Se nenhuma for encontrada, lança `AuthenticationError`.

## Método principal

```python
client.chat(
    prompt, *,
    chat_session_id=None, parent_message_id=None,
    model_type="default", thinking_enabled=False, search_enabled=True,
    ref_file_ids=None, attachments=None, action=None, preempt=False,
    on_text=None, on_thinking=None, on_chunk=None,
    auto_continue=True, max_continue_attempts=20, create_session=True
) -> ChatResult
```

Se `chat_session_id` não existir, cria uma nova sessão (exceto `create_session=False`, quando gera `ValueError`). `parent_message_id` identifica o pai do novo turno. `on_text(delta)`, `on_thinking(delta)` e `on_chunk(StreamChunk)` recebem eventos enquanto o servidor responde.

| Argumento | Padrão | Função |
| --- | --- | --- |
| `thinking_enabled` | `False` | Ativa raciocínio |
| `search_enabled` | `True` | Liga busca da interface |
| `auto_continue` | `True` | Retoma automaticamente `INCOMPLETE` |
| `max_continue_attempts` | `20` | Teto de proteção, não previsão de quantos turnos serão necessários |
| `model_type` | `default` | Campo privado; não há catálogo público garantido |
| `ref_file_ids` | `None` | IDs de arquivo remoto já processados |
| `attachments` | `None` | Lista de caminhos, bytes nomeados ou arquivos locais a enviar e processar antes da geração |
| `action`, `preempt` | opcionais | Campos passados ao endpoint |

`ChatResult`: `session_id: str`, `text: str`, `thinking: str`, `title: str|None`, `status: str|None`, `request_message_id: int|None`, `response_message_id: int|None`, `events: int`, `continuations: int`, `file_ids: list[str]`, `usage: dict` (campo reservado, não preenche tokens contabilizados).

## Interface incremental

```python
client.stream_completion(
    chat_session_id="UUID", prompt="Mensagem",
    parent_message_id=None, model_type="default",
    thinking_enabled=False, search_enabled=True,
    ref_file_ids=None, attachments=None, action=None, preempt=False,
    auto_continue=True, max_continue_attempts=20
) -> Iterator[StreamChunk]
```

Não cria sessão, exige UUID existente. Cada `StreamChunk` contém `event`, `text`, `thinking`, `title`, `status`, `done`, `raw`, `attempt`, `file_id` e `filename`. Antes da geração, cada anexo concluído emite um evento `file`.

### Métodos para mídias

- `upload_file(file, *, filename=None, mime_type=None, model_type="default", thinking_enabled=False, wait_for_processing=True, parse_timeout=300.0, poll_interval=3.0)`: aceita caminho local, bytes acompanhados por `filename` ou tupla `(filename, bytes)`. Envia multipart e retorna `{"file_id", "filename", "size", "mime_type", "status", "file_info"}`; `file_info` só aparece quando a espera está ativada.
- `wait_for_file_ready(file_id, *, timeout=300.0, poll_interval=3.0)`: consulta o status até `SUCCESS` ou erro.
- `upload_files(files, **kwargs)`: envia uma sequência e retorna os resultados na ordem recebida.

Consulte [MIDIAS.md](MIDIAS.md) para formatos, endpoints e exemplos completos.

Outras interfaces: `create_chat_session()`, `create_pow_challenge(target_path, session_id=None)`, `create_completion_stream(...)`, `continue_completion_stream(chat_session_id, message_id, fallback_to_resume=True)`. As duas funções de stream HTTP de baixo nível retornam `requests.Response`, que deve ser fechado pelo consumidor.

Erros: `AuthenticationError`, `ProofOfWorkError`, `DeepSeekAPIError` (status HTTP, código e trace id quando disponíveis); erros `requests` por rede/timeouts.

## CLI

```text
python [-u] api_client.py [OPTIONS] [PROMPT]
```

| Opção | Resultado |
| --- | --- |
| `--stream` ou `--jsonl` | Eventos JSON Lines, flush a cada linha |
| `--json` | Resposta completa em JSON |
| `--thinking` | Ativa thinking |
| `--file PATH` | Anexa um arquivo local; repetível para vários arquivos |
| `--show-thinking` | Mostra thinking em stderr (modo texto) |
| `--no-search` | Desliga busca web |
| `--session-id UUID` / `--parent-message-id N` | Novo turno em conversa existente |
| `--no-auto-continue` | Desliga continuação de resposta incompleta |
| `--max-continues N` | Teto de continuidade (20 por padrão) |
| `--model NOME` | Campo `model_type` |
| `--token`, `--device-id` | Sobrescritas de autenticação/dispositivo |
| `--timeout`, `--stream-timeout` | Timeouts em segundos |
| `--selftest`, `--forget-credentials`, `--quiet` | Testar, apagar credencial, reduzir stderr |

Se prompt não for passado e stdin não for TTY, lê o prompt da entrada padrão. Falhas retornam status diferente de zero e texto de erro em stderr.

## Contrato JSONL

```jsonl
{"type":"file","file_id":"ID_REMOTO","filename":"imagem.png","status":"SUCCESS"}
{"type":"thinking","delta":"..."}
{"type":"text","delta":"Olá"}
{"type":"title","title":"Título"}
{"type":"continue","attempt":1}
{"type":"done","session_id":"UUID","status":"FINISHED","request_message_id":1,"response_message_id":2,"events":100,"continuations":1,"file_ids":["ID_REMOTO"]}
```

Esses são exemplos de formato, não uma transcrição real. `file`, `thinking`, `title` e `continue` são opcionais. O evento final `done` ocorre apenas se `chat()` termina normalmente.

## Exemplos

```python
from api_client import DeepSeekClient
cli = DeepSeekClient()
primeira = cli.chat("Escreva 3 dicas de estudo.", on_text=lambda t: print(t, end="", flush=True))
proxima = cli.chat(
    "Explique a segunda dica.",
    chat_session_id=primeira.session_id,
    parent_message_id=primeira.response_message_id,
)
print(proxima.text)
```

Também consulte `examples/`.
