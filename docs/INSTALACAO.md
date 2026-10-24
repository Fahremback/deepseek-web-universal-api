# Instalação e início rápido

## Requisitos

- Python 3.10+.
- Rede HTTPS para `https://chat.deepseek.com`.
- Dependências `requests>=2.31` e `keyring>=25`.
- Uma sessão já autenticada ou bearer token válido, fornecido pelo titular.
- O solucionador de PoW `ds_pow_solver.exe`, compilador C para gerar equivalente ou fallback Python mais lento.

```powershell
python -m pip install -r requirements.txt
python api_client.py --selftest
```

## Primeira autenticação (sem navegador em execução)

Em uma máquina que tenha dados de uma sessão DeepSeek anterior armazenados em Edge/Chrome:

```powershell
python import_existing_session.py
```

O script lê arquivos locais via leitores offline opcionais de `deepseek_api/tools`, valida o bearer com `GET /api/v0/users/current` e salva no cofre de credenciais do sistema. **Não inicia navegador.** É apenas importador de sessão anterior, não pré-requisito de execução da API.

Em máquinas sem esse perfil, o titular fornece o bearer uma única vez:

```powershell
$env:DEEPSEEK_TOKEN = "TOKEN_VALIDO"
python api_client.py --json "Responda apenas OK."
Remove-Item Env:DEEPSEEK_TOKEN
```

Também são aceitos `--token` e `DEEPSEEK_TOKEN_FILE`. Após uma chamada autenticada bem-sucedida, o cliente armazena a credencial via `keyring`. Não há fluxo implementado de login por senha ou emissão autônoma de token.

## Primeira mensagem

```powershell
python -u api_client.py "Explique gravidade."
python -u api_client.py --stream --thinking "Explique gravidade."
python api_client.py --json "Responda OK."
```

O primeiro comando imprime texto incremental, o segundo emite JSONL imediato e o terceiro um JSON completo ao final. `--thinking` é opcional.

## Enviar imagens e documentos

```powershell
python -u api_client.py --stream --file "foto.png" "Descreva esta imagem."
python -u api_client.py --stream --file "relatorio.pdf" --file "grafico.jpg" "Resuma o documento e analise o gráfico."
```

`--file` aceita caminhos locais explícitos e pode ser repetido. Se não houver prompt, o cliente utiliza a frase "Analise os arquivos anexados.". O fluxo carrega os arquivos via HTTP, aguarda o processamento remoto e só então envia o prompt com os identificadores obtidos. A mídia é enviada à conta DeepSeek autenticada. [Detalhes e exemplos Python](MIDIAS.md).

## Continuação de conversa anterior

```powershell
python -u api_client.py --stream --session-id UUID_ANTERIOR --parent-message-id 2 "Continue esse assunto."
```

O ID 2 é somente exemplo. Use os campos `session_id` e `response_message_id` da resposta anterior.

## Remover acesso local

```powershell
python api_client.py --forget-credentials
```

Não registre nem publique tokens, cookies ou cabeçalhos de autenticação.
