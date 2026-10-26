# Instalação e início rápido

## Requisitos

- Python 3.10+.
- Rede HTTPS para `https://chat.deepseek.com`.
- Dependências `requests>=2.31` e `keyring>=25`.
- Uma sessão já autenticada ou bearer token válido, fornecido pelo titular.
- O solucionador de PoW compatível com o sistema (em Windows, `ds_pow_solver.exe`), compilador C para gerar equivalente ou fallback Python mais lento.

### Portabilidade entre computadores e navegadores

O cliente HTTP funciona sem navegador depois que uma sessão válida foi configurada. O código Python suporta Windows, macOS e Linux; o cofre usa o backend do `keyring` disponível no sistema (em Linux sem serviço de segredos, use provisionamento explícito por `DEEPSEEK_TOKEN_FILE`). O solver PoW verifica executáveis já disponíveis, compila C genérico no cache do usuário quando necessário ou recorre a Python puro, mais lento. A compilação não exige permissão de escrita no diretório do código e não reaproveita automaticamente um binário de outra plataforma. É necessário um compilador C para desempenho nativo em sistemas sem executável compatível.

O importador opcional busca perfis Chromium locais de Comet, Edge, Chrome, Brave, Chromium e Vivaldi em diretórios típicos de cada sistema. Para outro navegador baseado em Chromium, `DEEPSEEK_BROWSER_PROFILE` aponta para o diretório de perfil (por exemplo, `Default`) ou para o diretório `Local Storage/leveldb`. A leitura utiliza primeiro `ccl_chromium_reader.ccl_chromium_localstorage`, caso esteja instalado no Python, e mantém compatibilidade com o leitor existente em `deepseek_api/tools`. O leitor não é distribuído automaticamente com esta pasta e sua disponibilidade não é necessária para chamadas HTTP quando uma sessão válida já foi configurada. Sem leitor compatível, faça login pelo navegador gerenciado pelo Playwright ou forneça uma credencial própria válida.

Para fazer login interativo em um **perfil gerenciado pelo projeto** e não depender do navegador padrão, instale os binários do Playwright usados pelo seu sistema:

```text
python -m pip install -r requirements.txt
python -m playwright install chromium firefox webkit
python verify_session.py --login-web --login-browser firefox --generate
```

Também são aceitos `--login-browser chromium`, `webkit`, `chrome` e `msedge` (os dois últimos exigem que o navegador correspondente esteja instalado). O login ocorre exclusivamente no site oficial, e as credenciais são armazenadas no keyring. Para usar o navegador padrão, omita `--login-browser`: ele é aberto pela associação de URL do sistema, mas a captura automática do token só funciona quando seu armazenamento local é suportado pelo importador. Firefox comum, Safari ou navegadores com perfis inacessíveis **não** têm captura genérica do perfil existente; use o fluxo controlado correspondente ou `--token`/`DEEPSEEK_TOKEN_FILE` fornecido pelo titular.

Os perfis gerenciados de Chrome e Edge são isolados: Chrome preserva a pasta legada `browser-profile` e Edge usa `browser-profile-msedge`. Se um login Edge anterior estava no perfil compartilhado antigo, o navegador Edge poderá exigir novo login interativo; tokens já salvos no cofre do sistema continuam disponíveis ao cliente HTTP.

Compatibilidade com qualquer hardware, qualquer navegador e qualquer desafio de login não pode ser garantida sem testar cada plataforma e sem suporte do site DeepSeek.

No macOS/Linux, use `python3` no lugar de `python` se esse for o nome do interpretador instalado. Os testes automatizados de plataforma usam a matriz Windows/macOS/Linux em `.github/workflows/tests.yml`; os testes de browser nessa matriz simulam o mecanismo, não executam login real nem capturam sessões de terceiros.

```powershell
python -m pip install -r requirements.txt
python api_client.py --selftest
```

## Primeira autenticação (sem navegador em execução)

Em uma máquina com sessão DeepSeek autenticada no Perplexity Comet (inclusive perfis secundários) ou Edge:

```powershell
python import_existing_session.py
```

O script lê a sessão local com os leitores de `deepseek_api/tools`, valida o bearer com `GET /api/v0/users/current` e os cabeçalhos do cliente HTTP, e salva no cofre de credenciais do sistema. **Não inicia navegador.** A importação e geração podem ser validadas com `python verify_session.py --live --generate`.

Para autenticar usando o navegador padrão, com reutilização automática da sessão existente no Comet:

```powershell
python verify_session.py --login-web --generate
```

Em máquinas sem esse perfil, o titular fornece o bearer uma única vez:

```powershell
$env:DEEPSEEK_TOKEN = "TOKEN_VALIDO"
python api_client.py --json "Responda apenas OK."
Remove-Item Env:DEEPSEEK_TOKEN
```

Também são aceitos `--token` e `DEEPSEEK_TOKEN_FILE`. Após uma chamada autenticada bem-sucedida, o cliente armazena a credencial via `keyring`. Existe um fluxo opcional de login por senha com credenciais guardadas separadamente no cofre do sistema; desafios interativos são resolvidos exclusivamente no site oficial.

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
