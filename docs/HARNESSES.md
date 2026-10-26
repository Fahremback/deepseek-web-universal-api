# API para harnesses: OpenAI e Anthropic compatíveis

O arquivo `openai_server.py` disponibiliza uma **API HTTP local** para usar o cliente DeepSeek Web como backend de ferramentas, agentes, editores e bibliotecas que aceitam protocolos OpenAI/Anthropic. Ele continua usando o protocolo privado autenticado do DeepSeek Web; não abre navegador e não depende da API comercial.

## Instalar e iniciar

Execute na pasta `deepseek_auth_20261024`:

```powershell
python -m pip install -r requirements.txt
python openai_server.py --host 127.0.0.1 --port 8000
```

A credencial DeepSeek Web existente é recuperada pelo `DeepSeekClient` do cofre do sistema. Para verificação:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/v1/models
```

Para proteger o serviço com uma chave local, defina-a **antes** de iniciar o servidor:

```powershell
$env:DEEPSEEK_BRIDGE_API_KEY = "segredo-local-forte"
python openai_server.py
```

Os clientes precisam enviar essa chave no cabeçalho `Authorization: Bearer` (OpenAI) ou `x-api-key` (Anthropic). Por padrão o servidor escuta apenas `127.0.0.1`. Escutar em outra interface exige uma chave. O tráfego HTTP não é criptografado; não o exponha diretamente à internet.

Para encaminhar modelos que não sejam DeepSeek para um gateway OpenAI/Responses já existente, use `--fallback-url`. Quando esse destino exige uma chave **diferente** da chave local, configure `DEEPSEEK_FALLBACK_API_KEY` no ambiente do processo. A chave de fallback substitui o cabeçalho Authorization somente nas chamadas encaminhadas; os modelos DeepSeek continuam usando a sessão do cofre do Windows. Sem essa variável, a autorização do cliente é mantida para compatibilidade com gateways que usam a mesma chave.

Quando um cliente no navegador usa uma origem local permitida, a mesma política CORS do gateway é aplicada também às respostas encaminhadas ao fallback, inclusive erros HTTP e SSE.

Mesmo quando o gateway original utiliza a mesma chave de API, cookies locais e credenciais de proxy (`Cookie` e `Proxy-Authorization`) nunca são repassados ao fallback.

## Configuração em um harness

### Codex CLI com ciclo de vida gerenciado

O comando `python run_codex.py -- exec "Sua tarefa"` configura o Codex CLI
somente para esta execução com um provedor `deepseek_web_local` do tipo
`responses`. Também é possível executar `python run_codex.py` para a interface
interativa. A configuração é passada por argumentos `-c`, sem alterar a
configuração global ou interromper outros proxies e modelos do usuário.

O gateway fica acessível apenas em `127.0.0.1`, recebe uma chave local temporária
via variável de ambiente compartilhada exclusivamente com o subprocesso Codex,
e é encerrado no final. O iniciador usa `Gateway(("127.0.0.1", 0))` dentro do
próprio processo para reservar atomicamente a porta até o encerramento; não há
intervalo de exposição entre escolher uma porta livre e iniciar o servidor.
Não é necessário iniciar um subprocesso Python adicional nem consultar `/health`
para verificar uma porta que poderia ter sido ocupada por outro processo.

Para respostas HTTP normais, o gateway comunica uma pausa local com HTTP 429
e `Retry-After`. Em streaming SSE, após enviar os cabeçalhos HTTP 200, não é
possível alterar o status: o cliente deve ler eventos `response.failed` (API
Responses) ou mensagens `error` (Chat Completions). Uma pausa é sinalizada
com `type=rate_limit_error` e inclui `retry_after` quando conhecido. Um 429
remoto também recebe o tipo `rate_limit_error`, sem repassar a mensagem bruta
do provedor.
O mesmo tratamento de pausa vale para SSE de `POST /v1/completions` (legado)
e `POST /v1/messages` (Anthropic): os eventos de erro incluem
`type=rate_limit_error` e `retry_after` quando o tempo de pausa local é conhecido.
O gateway aguarda no máximo 350 ms pela vaga de concorrência, mantendo o
limite de uma requisição ativa por padrão. Se ela não liberar, devolve
HTTP 429 com `Retry-After: 1`, sem iniciar uma geração adicional.
O `run_codex.py` ativa um limite específico de até 1.000.000 de caracteres
no prompt preparado durante a sua sessão, necessário para instruções e
catálogos de ferramentas grandes. O gateway comum conserva seu limite de
200.000 caracteres, e a variável temporária é restaurada quando o Codex sai.
Quando o estado persistido dos limites locais está inacessível, os endpoints
JSON devolvem HTTP 503 com `Retry-After: 60`. Se o streaming SSE já começou,
os eventos de erro usam `type=rate_limit_state_error` e `retry_after=60`, sem
detalhes de arquivos ou mensagens internas.
O comando pressupõe que o Codex CLI esteja **executável** no `PATH` e que uma
sessão válida esteja configurada no cofre do sistema. A descoberta verifica
`codex --version`, evitando atalhos incompatíveis (por exemplo, um shim npm do
Windows visto pelo WSL sem Node.js). Além dos testes simulados, o iniciador foi
testado com `codex --help` real no Windows, com saída 0 e a porta liberada após
a sessão. Isso não substitui um teste remoto real de Responses nem configura o
aplicativo Codex Desktop.

Para sistemas que aceitam **OpenAI-compatible**:

| Campo no harness | Valor |
| --- | --- |
| Provider | OpenAI-compatible / Custom OpenAI |
| Base URL | `http://127.0.0.1:8000/v1` |
| API key | Valor de `DEEPSEEK_BRIDGE_API_KEY`, ou qualquer texto se a chave local estiver desligada |
| Model | `deepseek-web` |
| Model com thinking | `deepseek-web-reasoner` |

Aliases aceitos: `deepseek-chat` e `deepseek-reasoner`. O modelo físico utilizado é o da interface web privada; esses IDs são nomes de compatibilidade do gateway.

Para sistemas que usam **Anthropic Messages**:

| Campo | Valor |
| --- | --- |
| Base URL | `http://127.0.0.1:8000` |
| API key | Mesmo segredo local |
| Model | `deepseek-web` (ou `deepseek-web-reasoner`) |

O protocolo Anthropic aceita nomes `claude-*` como aliases, roteados ao **DeepSeek Web**, para permitir configurações de harness que exigem esse prefixo. Não significa que a resposta venha de um modelo Claude.

O gateway responde também aos preflights CORS de clientes OpenAI e Anthropic executados no navegador em origens `localhost`/`127.0.0.1`.

## Exemplo: SDK oficial OpenAI

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="segredo-local-forte")
response = client.chat.completions.create(
    model="deepseek-web",
    messages=[{"role": "user", "content": "Diga OK"}],
)
print(response.choices[0].message.content)
```

Streaming:

```python
for part in client.chat.completions.create(
    model="deepseek-web-reasoner",
    messages=[{"role": "user", "content": "Explique grafos."}],
    stream=True,
):
    print(part.choices[0].delta.content or "", end="", flush=True)
```

## Ferramentas (tool calling)

O harness envia `tools` e `tool_choice`. O gateway instrui o modelo a produzir uma chamada JSON, verifica nome, argumentos e tipos básicos do schema e devolve `tool_calls` no formato esperado. O harness **executa** a ferramenta e envia uma nova solicitação com a mensagem `role=tool` / `tool_call_id` (OpenAI), ou bloco `tool_result` (Anthropic). O gateway transmite esse resultado ao DeepSeek no contexto da nova solicitação.

O DeepSeek Web **não oferece tool calling nativo nessa implementação**: as chamadas são estruturadas por prompting e validação. Um modelo pode falhar ao obedecer ao formato; uma chamada obrigatória inválida é tentada novamente uma vez e então devolve erro HTTP 502, nunca uma ferramenta inventada.

Com `stream=true`, texto sem ferramentas flui incrementalmente. Na presença de `tools`, o gateway espera a resposta do modelo, valida o bloco e só então emite os deltas da chamada; isso evita entregar JSON de ferramentas como texto parcial.

Para `response_format` de Chat Completions ou `text.format` de Responses com `json_object`/`json_schema`, a resposta é validada (sintaxe JSON, campos obrigatórios e tipos básicos). Se inválida, há uma nova tentativa; falha persistente gera 502. Quando esse formato é solicitado, o streaming é emitido depois da validação.

## Endpoints

| Rota | Protocolo |
| --- | --- |
| `GET /health` | Verificação local |
| `GET /v1/models` e `GET /v1/models/{id}` | Listagem e descrição dos aliases |
| `POST /v1/chat/completions` | OpenAI Chat Completions, inclusive SSE |
| `POST /v1/completions` | Completions legado |
| `POST /v1/responses` | OpenAI Responses, inclusive SSE |
| `GET /v1/responses/{id}` | Consulta de resposta recente |
| `POST /v1/messages` | Anthropic Messages, inclusive SSE e tool use |
| `POST /v1/messages/count_tokens` | Estimativa local de tokens (não contagem real) |

Na Responses API, `previous_response_id` recompõe as mensagens anteriores de um cache em memória limitado a 256 respostas, perdido quando o serviço reinicia. Em Chat Completions, o cliente deve mandar o histórico completo em `messages`, como exige o protocolo comum; o gateway abre uma conversa DeepSeek Web nova por solicitação.

Resultados de ferramentas na Responses API exigem `call_id` não vazio e aceitam `output` como texto ou lista de partes de conteúdo suportadas (por exemplo, `input_text`). A lista é convertida pela camada normal de conteúdo; não é serializada como representação textual Python.

## Mídia

- `image_url` com URI `data:image/...;base64,...` e partes `file` em `data:application/pdf;base64,...` são transformadas em anexos.
- Anthropic `image` e `document` com `source.type=base64` também são aceitos.
- URLs externas de imagens são recusadas; o servidor não baixa links arbitrários.
- Limites locais: até 12 anexos, 12 MiB por anexo e 32 MiB no total. O serviço DeepSeek pode impor limites inferiores.
- PNG e PDF foram testados na conta remota. Outros formatos não tiveram validação remota.

## Limites reais

- Os papéis `system`/`developer` e as chamadas de ferramentas são convertidos em texto estruturado para o modelo. **Não equivalem ao controle nativo de papéis/ferramentas de uma API oficial.**
- Parâmetros como `temperature`, `top_p`, `seed`, `max_tokens` e `max_completion_tokens` não controlam o backend web atual, embora possam ser enviados por clientes sem quebrar a chamada.
- O DeepSeek Web não informa métricas padronizadas de tokens aqui. `usage` (OpenAI/Responses/Anthropic) e `count_tokens` são **estimativas por caracteres**, não contagens reais; o gateway marca respostas OpenAI com `x_deepseek_usage_estimated: true`.
- O funcionamento remoto depende de credencial válida, estabilidade das rotas privadas, PoW e eventuais limites do provedor.
- Embeddings, geração de áudio, batching e outros endpoints de provedores não estão implementados. Harnesses que exigem exclusivamente essas capacidades precisam de backend adicional.

## Testes

```powershell
python -m unittest -q test_openai_bridge test_media test_auto_continue test_streaming test_credential_store
python -u examples/live_gateway_smoke.py
```

O primeiro comando usa respostas simuladas localmente, incluindo os SDKs oficiais quando instalados. O segundo faz chamadas reais autenticadas ao DeepSeek Web e verifica texto, invocação de ferramenta e resposta com resultado da ferramenta; ele usa apenas dados sintéticos.
