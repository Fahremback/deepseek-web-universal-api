# Testes, observações e reprodução

## Verificação local em 26/10/2026

- `python -m unittest discover -p 'test_*.py' -q`: **199 testes registrados** nesta cópia de trabalho (Windows: 197 aprovados, 2 ignorados; Ubuntu/WSL: 195 aprovados, 4 ignorados). A suíte completa foi executada nos dois sistemas em 26/10/2026, sem falhas.
- O iniciador `run_codex.py` agora ativa `DEEPSEEK_CODEX_BRIDGE=1` durante sua sessão e restaura o valor anterior ao encerrar, mesmo após erro. O adaptador consulta essa configuração no momento de cada requisição, porque o gateway é carregado no mesmo processo do iniciador. Um teste HTTP real confirma que o Codex aceita um prompt de 250 mil caracteres com o backend simulado, enquanto o limite de 200 mil caracteres dos demais clientes permanece inalterado. Os 199 testes foram reexecutados após essa correção.
- **Codex CLI real com DeepSeek remoto validado em 26/10/2026 no Windows:** `verify_session.py --live` confirmou autenticação (`authorization=accepted`, HTTP 200). Em seguida, `run_codex.py` lançou o Codex CLI com o gateway Responses local e sessão temporária, sem plugins nem aplicativos externos; uma solicitação curta ao modelo remoto concluiu com código de saída 0 e resposta exata `PONTE_OK`. O gateway registrou HTTP 200. O ensaio não exercitou ferramentas executáveis, chamadas longas, multimodalidade, outros sistemas operacionais nem uso contínuo. O stderr do Codex continha avisos e referência a 429, mas o estado do limitador HTTP do DeepSeek não registrou 429/403 nem pausa; a origem desses avisos não foi confirmada. O ensaio limitou a conta a 6 chamadas/hora e 15 chamadas/dia nesse processo, com intervalo mínimo de 3 segundos.
- `python api_client.py --selftest`: vetores PoW, solver nativo e reconstrução SSE aprovados.
- Em Ubuntu 26.04 LTS no WSL, Python 3.14.4: suíte offline completa **123/123 aprovada**; compilação Python aprovada e solver PoW nativo compilado com GCC e testado (`answer=50837`). O ensaio usou virtualenv Linux separado e não necessitou importar credenciais do Windows; portanto, **não** é prova de login DeepSeek ao vivo no Linux.
- `python import_existing_session.py`: importação de sessão válida do Comet aprovada após a sessão anterior ter retornado `api_code=40003`.
- `python verify_session.py --live --generate`: `authorization=accepted`, `generation=accepted`, `http_status=200` após reimportação.
- Testes novos cobrem a limpeza de cookies e prova de sessão quando o token do cofre é trocado, autenticação separada para encaminhamento a outro gateway OpenAI e reimportação imediata de sessões válidas no Comet sem espera indevida de 60 segundos.
- O streaming de Responses agora preserva os IDs de itens de texto e chamadas de ferramentas entre os eventos `response.output_item.added`, `response.output_item.done` e o `response.completed` final. O importador Comet também revalida sem espera quando há alteração de prova/dispositivo mesmo com bearer token inalterado.
- O verificador `python verify_session.py --live` reimporta uma sessão Comet autenticada se o token salvo for rejeitado, sem abrir navegador; o `verify_authorization()` usado para conferir candidatos permanece um teste isolado e não realiza recuperação recursiva.
- Os erros do provedor agora retornam mensagens HTTP genéricas e não repassam dados brutos do DeepSeek. O adaptador stdin/stdout também não imprime descrições de exceções contendo respostas privadas no stderr; há regressões automatizadas para ambos.
- O fallback de modelos não-DeepSeek agora mantém os cabeçalhos CORS gerados pelo gateway local para HTTP 200, erros e streaming, sem aceitar uma política CORS conflitante do servidor de destino; o cabeçalho `Vary` original também é preservado.
- O fallback descarta sempre os cabeçalhos `Cookie` e `Proxy-Authorization` da requisição de entrada. Um teste HTTP ponta a ponta verifica que nenhum deles é transmitido ao destino, mantendo a autorização de API quando a configuração prevê reutilizá-la.
- O cliente DeepSeek reaplica uma sessão Comet renovada mesmo se o bearer token não mudar, desde que a prova `hif_leim`, o dispositivo ou os cookies mudem. Regressões offline cobrem tanto a atualização de credenciais quanto o retry após erro de autenticação `code=40003`.
- O adaptador `codex_stdio.py` retorna código de saída 1 quando o SSE indica `response.failed`, em vez de indicar sucesso ao Codex; fluxos `response.completed` continuam retornando 0. Parâmetros `stream` não booleanos são recusados antes de iniciar a geração. Novos testes validam os três casos.
- O login controlado oferece Chromium, Firefox e WebKit via Playwright além de Chrome/Edge, com perfis separados e diretórios de dados específicos de Windows/macOS/Linux. O leitor opcional procura vários navegadores Chromium e aceita `DEEPSEEK_BROWSER_PROFILE` para perfis customizados. Testes simulam os motores e diretórios em outras plataformas; **não substituem ensaios reais em cada sistema**.
- A configuração de CI agora prevê a suíte offline completa em Windows, macOS e Ubuntu, com Python 3.10, 3.11, 3.12 e 3.14. Os trabalhos remotos ainda não foram executados nesta revisão; a validação efetivamente observada foi no Windows (Python 3.11) e no Ubuntu/WSL (Python 3.14).
- A importação de sessão aceita um leitor Chromium instalado como pacote Python mesmo quando o diretório `deepseek_api/tools` não existe; mantém a alternativa local e informa indisponibilidade quando nenhum leitor está presente. O teste de descoberta do Comet deixou de depender de um caminho exclusivo do Windows.
- Um token fornecido explicitamente ou pelo ambiente nunca herda prova de WAF, cookies ou identificador de dispositivo de uma conta diferente salva no keyring. Um token que corresponde ao salvo continua reutilizando os dados válidos; ambas as situações têm testes de regressão.
- Um ensaio real no Ubuntu/WSL com arquivos Python, fonte C e requisitos copiados para `/tmp`, sem `.git`, credenciais nem executável PoW pré-compilado, passou a suíte completa de 123 testes então disponível e compilou o solver nativo. Um novo teste permanente executa importações e hashing em uma pasta temporária isolada, sem a estrutura de diretórios do projeto. Perfis gerenciados Chrome e Edge deixaram de compartilhar o mesmo diretório, preservando o caminho legado do Chrome.
- O solver PoW agora valida um binário encontrado usando um vetor conhecido antes da primeira utilização; descarta binários incompatíveis e compila C genérico (`-O3`, sem `-march=native`) em um cache do usuário identificado por sistema, arquitetura e hash do código-fonte. Não grava mais código-fonte ou executáveis na pasta instalada. Testes de regressão cobrem binário incompatível, ausência de compilador, separação por CPU e compilação nativa real quando há GCC/Clang/CC, sem fonte C nem executável distribuído.
- A cada desafio, o nonce do solver nativo é conferido pelo hash real: saída incorreta ou erro do processo aciona fallback Python, em vez de gerar um cabeçalho PoW inválido. Um teste HTTP local de dois turnos verifica a continuidade de Responses com `previous_response_id` e `function_call_output`; outro faz o mesmo para `custom_tool_call_output` com streaming. Nenhum deles consulta o DeepSeek remoto.
- Os resultados `function_call_output`/`custom_tool_call_output` da API Responses agora preservam blocos `input_text` (e demais partes compatíveis) como conteúdo estruturado, em vez de convertê-los em representações Python de listas. IDs `call_id` ausentes ou vazios são recusados antes de fazer uma chamada ao backend. Testes unitários e HTTP locais cobrem ambos os casos.
- Um arquivo de estado de requisições ausente é tratado como primeira execução, mas um arquivo existente e corrompido (JSON inválido, valores negativos, NaN ou infinito) interrompe a requisição antes da rede. Um teste com `DeepSeekClient` comprova que um HTTP 429 recebido continua impedindo chamadas depois de instanciar outro cliente. Os endpoints HTTP do gateway retornam 429 e `Retry-After` quando há pausa ativa, 429 para limite remoto inicial e 503 quando os controles locais estão inacessíveis, sem refletir mensagens privadas do provedor.
- Os endpoints de streaming legado `/v1/completions` e Anthropic `/v1/messages` também sinalizam `rate_limit_error`, incluindo `retry_after` quando a pausa local é conhecida. Um HTTP 429 inicial vindo do provedor produz erro SSE sanitizado, sem repassar o texto privado da resposta remota.
- O gateway aguarda brevemente (até 350 ms) a liberação do semáforo antes de responder com HTTP 429 por concorrência, evitando rejeições espúrias de chamadas sucessivas. Se a vaga continuar ocupada, devolve 429 com `Retry-After: 1`. Testes HTTP cobrem ambos os caminhos sem permitir mais de uma execução simultânea por padrão.
- O gateway sinaliza arquivo de controle de limites inacessível como HTTP 503 com `Retry-After: 60` em respostas normais. Nos quatro modos SSE (Responses, Chat Completions, legado e Anthropic) emite `rate_limit_state_error` e `retry_after: 60`, sem refletir erros locais privados. Dois testes HTTP cobrem os cinco resultados; os 53 testes do gateway passaram no Windows e Ubuntu/WSL.
- O iniciador opcional `run_codex.py` fornece um provedor Responses ao Codex CLI usando argumentos temporários, chave local aleatória em variável de ambiente e gateway em thread encerrado em `finally`. O validador do Codex CLI aceitou o formato das opções de provedor (`config.load: ok`). Um ensaio real `python run_codex.py -- --help` no Windows iniciou o Codex CLI, terminou com código 0 e confirmou que a porta temporária foi liberada. No Ubuntu/WSL, o atalho `codex` do Windows falhou por ausência de Node.js; a descoberta agora rejeita atalhos não executáveis e ignora o teste real nessa instalação.
- O iniciador passou a hospedar o gateway HTTP em uma thread do mesmo processo. O sistema atribui e mantém ocupada a porta efêmera sem a janela de corrida anterior. Os testes verificam, com HTTP de verdade, rejeição de acesso sem chave (401), acesso com chave (200), impossibilidade de outro processo ocupar a porta enquanto o Codex é iniciado e liberação da porta ao sair ou falhar. Essa validação não depende da API DeepSeek remota.
- Um teste adicional executa o iniciador inteiro com o **Codex CLI real** e o gateway real, substituindo apenas o modelo remoto por um backend simulado. O protocolo Responses retorna o texto esperado e a sessão termina corretamente. Plugins e apps do Codex são desativados **somente neste teste** para evitar sincronizações assíncronas que mantêm arquivos temporários abertos. Isso comprova o percurso Codex → gateway, mas ainda não valida geração com o DeepSeek remoto.
- Uma chamada real anterior pelo gateway `/v1/chat/completions` respondeu HTTP 200 e concluiu a geração. Isso não comprova todos os protocolos ou a disponibilidade contínua do serviço.
- Iniciar um servidor persistente e executar novas chamadas reais aos endpoints Responses/Anthropic continuam sem validação, porque essas operações foram recusadas pelas configurações de segurança do ambiente.

## Suíte offline

Executar na pasta do projeto:

```powershell
python -m py_compile api_client.py harness_adapter.py anthropic_adapter.py openai_server.py test_media.py test_openai_bridge.py
python api_client.py --selftest
python -m unittest -q test_openai_bridge test_media test_auto_continue test_streaming test_credential_store
```

Observado nesta implementação, em 24/10/2026:

- Compilação Python: aprovada.
- `--selftest`: vetores PoW, solver e fixtures SSE aprovados.
- `unittest`: **39 testes aprovados**, contemplando gateway OpenAI/Anthropic, SDKs oficiais, tool calling, streaming SSE, `usage` estimado, JSON estruturado, CORS Anthropic, Responses `previous_response_id`, anexos, armazenamento de credenciais, parsing SSE e continuidade automática.

## Testes reais executados anteriormente

| Cenário | Resultado observado |
| --- | --- |
| Geração longa, sem thinking | `FINISHED`, **38.391** caracteres, **1** `/continue` |
| Conferência da geração longa contra histórico do servidor | 38.391 caracteres recebidos, 38.391 recuperados, correspondência exata |
| Geração curta com thinking | `FINISHED`, 151 caracteres de resposta e 267 de thinking |
| Conferência curta com thinking | Texto e thinking compatíveis exatamente com o histórico |
| Geração longa com thinking antes do reparo de deduplicação | 1 continuação; revelou replay duplicado, motivando correção; não constitui validação pós-correção do mesmo cenário |
| Imagem PNG sintética vermelha enviada por multipart | `FINISHED`, 1 arquivo referenciado, resposta `Vermelho` |
| Documento PDF sintético com palavra de verificação | `FINISHED`, 1 arquivo referenciado, resposta `JABUTI42` |
| Gateway local OpenAI Chat Completions | HTTP 200, resposta `PONTE_OK` com DeepSeek real |
| Tool calling via gateway e DeepSeek real | `sum_two_integers(a=7,b=11)` validada |
| Resultado da ferramenta reenviado e resposta em streaming | Resposta `18` |
| Resposta JSON estruturada via gateway real | Schema `answer=PONTE_OK` validado |
| SDKs oficiais OpenAI e Anthropic (backend falso local) | Métodos comuns, SSE e tools aprovados |

A conferência de histórico utilizou `GET /api/v0/chat/history_messages?chat_session_id=...`, por chamada autenticada de diagnóstico. Este GET não é exposto como método público do cliente atual.

Amostras de sucesso são evidências pontuais, **não benchmark estatístico**. Não há distribuição de latências, throughput confiável ou comparação reprodutível com browser/Playwright.

## Repetição end-to-end

```powershell
python -u api_client.py --stream --thinking "Responda apenas OK."
python -u api_client.py --stream --file "foto.png" "Descreva esta imagem."
```

Espera-se fluxo JSONL com `done`, `status` e IDs de mensagens. O modo thinking pode não retornar segmento visível de raciocínio dependendo do servidor.

Para testar `/continue` é preciso obter uma geração suficientemente longa para o servidor devolver `INCOMPLETE`. Isso depende do limite efetivo do modelo e não pode ser imposto pelo cliente. Conferir `continue` e `continuations` é suficiente para observar chamadas extras; comparar com histórico remoto é uma verificação mais forte.

## Critérios de regressão

- Não duplicar prefixos após replay/reset de `/continue`.
- Não perder texto quando um novo fragmento `RESPONSE` sucede `THINK`.
- Em `INCOMPLETE`, repetir `/continue` automaticamente até `FINISHED` ou erro explícito.
- Emitir `done` uma vez, no sucesso.
- Preservar `session_id`, `request_message_id` e `response_message_id`.
- Calcular PoW específico do endpoint de upload, aguardar `SUCCESS` e incluir novos IDs junto dos `ref_file_ids` existentes.
- Emitir `file` no JSONL de anexos sem perder `done.file_ids`.
- Nunca iniciar navegador, imprimir segredo nem tratar HTTP 200 com erro lógico como sucesso.

## Verificação adicional (25/10/2026)

`python -m unittest discover -p 'test_*.py' -q` passou com **80 testes**.
`python api_client.py --selftest` também passou, incluindo o solver PoW e a
reconstrução SSE. Os novos cenários cobrem descompressão gzip limitada ao tamanho
máximo de requisição, rejeição de gzip danificado, erros HTTP com CORS apenas
para origens locais permitidas e fechamento de respostas HTTP tanto em sucesso
quanto em falhas. `test_fallback_routing.py` verifica o encaminhamento de modelos
não DeepSeek ao gateway original, preservação de streaming, status HTTP,
compressão gzip, autenticação local e sigilo de logs. Erros encerram a conexão
HTTP para impedir que um corpo não consumido seja interpretado como uma nova
requisição. São testes offline, sem validar acesso à conta DeepSeek.

## Diagnóstico da sessão e validação real pelo titular

`python verify_session.py` verifica apenas a existência de uma sessão no cofre
local. Não faz requisições externas nem exibe token, senha ou identificação.

Após entrar em uma conta autorizada pelo site oficial, execute uma vez:

```powershell
python verify_session.py --login-web --generate
```

O navegador abre o login oficial. Após a autenticação e o salvamento da sessão,
o comando consulta `GET /api/v0/users/current` uma vez e, somente se for aceito,
executa uma geração curta. O JSON informa `authorization`, `http_status` e
`generation`, sem expor a resposta gerada. Se o login não termina, o comando
não reutiliza a sessão antiga para executar o teste.

`python verify_session.py --live` permite validar manualmente uma sessão já
confirmada, sem geração. Testes locais não atestam estabilidade ou autorização
futura do serviço.
