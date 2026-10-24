# Mídias e arquivos: upload por HTTP

O cliente envia anexos à conta DeepSeek Web autenticada **sem navegador**. O arquivo é carregado como multipart HTTP, processado pelo serviço e associado ao prompt por `ref_file_ids`.

## Linha de comando

```powershell
python -u api_client.py --stream --file "foto.png" "Descreva a imagem."
python -u api_client.py --stream --file "contrato.pdf" --file "foto.jpg" "Analise os dois anexos."
python -u api_client.py --file "relatorio.pdf" --json
```

- Repita `--file` para múltiplos caminhos locais. O caminho pode ser absoluto ou relativo à pasta de execução.
- Sem texto de prompt, a CLI utiliza `Analise os arquivos anexados.`.
- `--stream` emite eventos `file` antes dos eventos `text`/`thinking` e encerra com `done.file_ids`.
- `--json` inclui `file_ids` no objeto final.

## Python: arquivos no disco

```python
from api_client import DeepSeekClient

client = DeepSeekClient()
resposta = client.chat(
    "Compare os documentos e descreva a figura.",
    attachments=["relatorio.pdf", "figura.png"],
)
print(resposta.text)
print(resposta.file_ids)
```

## Python: conteúdo em memória

```python
from api_client import DeepSeekClient

arquivo = b"%PDF..."  # Substitua por bytes reais do PDF.
client = DeepSeekClient()
resposta = client.chat(
    "Resuma este documento.",
    attachments=[("documento.pdf", arquivo)],
)
print(resposta.text)
```

Bytes sem nome podem ser enviados diretamente por `client.upload_file(bytes_do_arquivo, filename="foto.png")`. O tipo MIME é identificado a partir do nome, salvo quando `mime_type` é passado explicitamente.

## Upload separado da geração

```python
from api_client import DeepSeekClient

client = DeepSeekClient()
anexo = client.upload_file("foto.png")
# anexo: file_id, filename, size, mime_type, status e file_info
resposta = client.chat("Descreva a imagem.", ref_file_ids=[anexo["file_id"]])
```

Use `client.upload_files(["foto.png", "contrato.pdf"])` para retornar uma lista ordenada de resultados. Com `wait_for_processing=True` (padrão), o upload só retorna após o servidor marcar o arquivo como `SUCCESS`.

`client.wait_for_file_ready(file_id, timeout=300.0, poll_interval=3.0)` permite aguardar separadamente. O método detecta estados de falha e lança `DeepSeekAPIError` em falha/timeout.

## Sequência HTTP implementada

1. `POST /api/v0/chat/create_pow_challenge` com `target_path: "/api/v0/file/upload_file"`.
2. `POST /api/v0/file/upload_file`, campo multipart `file`, cabeçalhos `x-ds-pow-response`, `x-file-size`, `x-model-type` e `x-thinking-enabled`.
3. `GET /api/v0/file/fetch_files?file_ids=ID` até `files[0].status == "SUCCESS"`; enquanto isso, o servidor pode retornar `PENDING` ou `PARSING`.
4. `POST /api/v0/chat/completion`, usando `ref_file_ids: ["ID"]` junto do prompt.

Cada upload recebe seu próprio desafio PoW. A geração possui outro desafio para `/api/v0/chat/completion`. O cliente mantém os IDs já fornecidos por `ref_file_ids` e adiciona os novos anexos.

## Suporte verificado e limites

Em **24/10/2026**, chamadas reais autenticadas confirmaram:

| Arquivo | Verificação | Resultado |
| --- | --- | --- |
| PNG | Imagem vermelha sintética, perguntando a cor | Resposta `Vermelho`; `FINISHED` |
| PDF | Texto `Palavra de verificacao: JABUTI42` | Resposta `JABUTI42`; `FINISHED` |

JPEG, outros formatos e limites máximos dependem das políticas atuais do DeepSeek Web e não foram validados nessas chamadas. A interface é **privada e mutável**, distinta da API pública em `api.deepseek.com`. O arquivo sai da máquina e é transmitido ao serviço remoto; use apenas conteúdo autorizado.
