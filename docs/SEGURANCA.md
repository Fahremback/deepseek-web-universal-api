# Segurança, erros, operação e limites

## Segurança de credenciais

O fluxo recomendado é `python api_client.py --login-web`: abre a página
**oficial** do DeepSeek no navegador padrão do Windows (Comet, Chrome ou Edge).
O usuário digita senha/2FA apenas no site. O leitor offline recupera somente o
token da origem `https://chat.deepseek.com`, valida-o por HTTP e guarda-o no
cofre do Windows. O navegador padrão permanece aberto e seu perfil usual é
preservado. `--login-browser chrome` e `--login-browser msedge` continuam
disponíveis para um perfil isolado em
`%LOCALAPPDATA%\DeepSeekWebClient\browser-profile`. Caso o navegador padrão não
seja compatível com o leitor, o programa não afirmará autenticação bem-sucedida.

1. O bearer é um segredo da conta. Nunca o inclua em código, relatório, captura, repositório, URL ou log.
2. `credential_store.py` usa `keyring` para gravar token, device id, prova `hif_leim` e cookies de sessão autorizados (`ds_session_id`, `HWWAFSESID`, `HWWAFSESTIME`), quando presentes.
   Os dados de sessão salvos só são carregados se o token usado no momento corresponder ao bearer armazenado; trocar de conta não mistura cookies ou provas de autenticação.
3. `import_existing_session.py` lê local storage offline **somente** dos perfis padrão Edge/Chrome e origem `https://chat.deepseek.com`, valida o token e então grava no cofre. Não abre navegador nem cria autenticação nova.
4. `--login` usa `/api/v0/users/login` para obter novo token via HTTP e guarda email/celular e senha em entrada **separada** do cofre do sistema. A senha é pedida sem eco no terminal (ou recebida por `--password-stdin` de forma explícita). Não há senha em código, `.env` nem argumentos de processo.
5. Para excluir token **e login salvo**: `python api_client.py --forget-credentials`. A exclusão não invalida token remoto; a revogação é responsabilidade do provedor/conta.
6. `DEEPSEEK_TOKEN`, `DEEPSEEK_TOKEN_FILE`, `--token`, `DEEPSEEK_DEVICE_ID`, `DEEPSEEK_COOKIES`, `DEEPSEEK_HIF_LEIM`, `DEEPSEEK_LOCALE` e `DEEPSEEK_TIMEZONE_OFFSET` permitem provisionamento/configuração explícita. Evite token na linha de comando compartilhada por histórico de terminal.

## Tratamento de falhas

| Condição | Comportamento implementado |
| --- | --- |
| Token ausente | `AuthenticationError` |
| HTTP 401/403 | Busca token mais recente no cofre; caso igual/ausente, realiza um novo login HTTP com a conta salva e repete a requisição no máximo uma vez; sem sucesso, `AuthenticationError` |
| Resposta JSON com `code`/`biz_code` não zero | `DeepSeekAPIError` com dados de diagnóstico |
| Conteúdo não SSE no endpoint de geração | Analisa erro JSON e lança exceção |
| Algoritmo PoW inesperado, dados inválidos ou nonce não encontrado | `ProofOfWorkError` |
| SSE fecha sem evento `close` | `DeepSeekAPIError` |
| `INCOMPLETE` além do teto de tentativas | `DeepSeekAPIError` |
| Upload ou processamento de arquivo falhou, ou expirou o tempo | `DeepSeekAPIError` |
| Falha de conexão ou timeout | Exceção do `requests` propagada à CLI |

## Cuidados de operação

### Limitação conservadora de requisições

`request_guard.py` administra **todas** as requisições emitidas por `DeepSeekClient`
(incluindo login, desafios PoW, streaming, upload e consulta de arquivos). O
espaçamento padrão é de 2 segundos, configurável por
`DEEPSEEK_MIN_REQUEST_INTERVAL`. Esse valor não representa um limite oficial
do DeepSeek. O controle vale por origem e é compartilhado entre processos
executados pelo mesmo usuário do sistema operacional.

Também há um **orçamento móvel conservador**, por origem: no máximo **90
requisições em 60 minutos e 400 em 24 horas** por padrão. Isso inclui chamadas
de login, PoW, streaming e uploads, não apenas solicitações de texto. O limite
é contabilizado *antes* do envio, sobrevive a reinicializações e é compartilhado
por processos locais pelo bloqueio de arquivo. Quando esgotado, o cliente
retorna uma pausa local sem enviar tráfego e o gateway comunica HTTP 429. Os
valores são medidas preventivas escolhidas pelo projeto, **não cotas oficiais
nem promessa de segurança da conta**. É possível torná-los mais rigorosos por
`DEEPSEEK_MAX_REQUESTS_PER_HOUR` e `DEEPSEEK_MAX_REQUESTS_PER_DAY`; valores
ausentes, inválidos, iguais a zero ou acima dos padrões não desativam os
limites. `DEEPSEEK_MIN_REQUEST_INTERVAL` também não pode reduzir o mínimo
abaixo de 2 segundos. Use a API oficial para volumes maiores ou serviços
automatizados em produção.

Respostas HTTP 429 suspendem novas chamadas pelo período informado em
`Retry-After` (ou por 60 segundos quando ausente). Respostas HTTP 403 criam
pausa preventiva de 10 minutos na ausência de outra instrução do servidor.
Falhas transitórias HTTP 500, 502, 503 e 504 aplicam 30 segundos de pausa
preventiva, ou seguem um `Retry-After` válido quando fornecido.
Bloqueios 429 ou 403 repetidos em até 24 horas aumentam progressivamente
o tempo mínimo de pausa (429: 1, 2, 4, 8... minutos até 1 hora;
403: 10, 20, 40... minutos até 24 horas). O valor maior entre a pausa
preventiva e o `Retry-After` do serviço sempre prevalece. O histórico guarda
somente um contador e o horário do último bloqueio, não os dados da conta.
Os prazos e a próxima reserva de requisição ficam no diretório privado de
estado do usuário (`DeepSeekWebClient/cooldown-*.json`), junto de horários de
requisições das últimas 24 horas, **sem tokens, cookies ou conteúdo enviado**.
Redirecionamentos HTTP automáticos estão desativados para impedir que uma
resposta 3xx provoque uma segunda requisição sem verificação dos limites ou
envie credenciais para outra origem. Valores inválidos ou não positivos em
`Retry-After` mantêm a pausa preventiva padrão; arquivos de estado existentes
sem ambos os campos obrigatórios são tratados como corrompidos.
Cada sessão autenticada também restringe requisições à mesma origem
(protocolo, domínio e porta) configurada, impedindo envio acidental de
credenciais para outra origem ou downgrade de HTTPS para HTTP. Domínios
equivalentes escritos com outras maiúsculas ou a porta HTTPS padrão explícita
compartilham a mesma pausa no disco, sem redefinir limites existentes.
As gravações são atômicas, protegidas por bloqueio de arquivo e sobrevivem ao
encerramento/reinício do gateway. Se o diretório de estado estiver inacessível,
o cliente interrompe a requisição em vez de ignorar os limites compartilhados.
Em Linux/macOS, o diretório próprio `DeepSeekWebClient` tem permissão `700`
e o arquivo de bloqueio tem permissão `600`. O JSON de estado é gravado por
arquivo temporário privado e substituição atômica. No Windows, a proteção
depende das permissões da pasta de dados do usuário.
Um arquivo de limites **existente e inválido** também causa interrupção segura;
somente a ausência de arquivo na primeira execução cria um estado inicial vazio.
Se um endpoint JSON responder com HTML, conteúdo não JSON ou um valor JSON
que não seja objeto, o cliente interrompe a operação em vez de registrar uma
resposta bem-sucedida. Páginas HTML recebidas durante o streaming também
produzem erro sanitizado, sem ecoar o conteúdo da página ou tentar renovar
automaticamente as credenciais. A conclusão de um CAPTCHA ou outra
verificação permanece restrita ao site oficial.
O gateway responde com HTTP 429 para limites ativos (com `Retry-After` se a
pausa local já está em curso) e HTTP 503 quando o controle local está inacessível.
Não remova esses arquivos para contornar pausas. Um CAPTCHA ou verificação de
conta deve ser resolvido no serviço oficial, não automaticamente pelo cliente.
O gateway permite somente **uma solicitação simultânea** por padrão; configurações
explícitas de `--max-parallel` podem alterar esse comportamento. Os orçamentos
locais são por origem e usuário do sistema, não agregam uso feito diretamente
no site, em outros dispositivos ou sob outra conta do sistema operacional.

Esses controles reduzem requisições excessivas, mas **não garantem** que uma
conta não será suspensa e não estabelecem permissão para usar endpoints privados.

- A interface web privada pode alterar cabeçalhos, campos, PoW, controles WAF, limites ou semântica sem aviso.
- Não há garantia de estabilidade, SLA, limites de taxa fixos, contagem oficial de tokens ou autenticação desassistida inicial.
- Um token inicialmente válido pode expirar/revogar. A reautenticação depende do login no cofre e do endpoint privado aceitar as credenciais; desafios adicionais/antibot podem exigir intervenção humana. Não há bypass de autenticação.
- O cliente não oferece serviço HTTP local, endpoints OpenAI-compatible, pesquisa de conversas nem persistência própria de histórico; a continuidade depende do UUID e ID da mensagem e do histórico remoto.
- O upload de mídia transmite o conteúdo integral do arquivo ao serviço remoto da conta autenticada. Envie apenas arquivos que podem ser tratados pelo serviço e evite incluir material secreto desnecessário; IDs de arquivo também podem identificar conteúdo.
- O cliente recebe caminhos explícitos de arquivos locais e não baixa automaticamente arquivos de URLs. Formatos admitidos, processamento e limites são determinados pelo servidor.
- A opção `search_enabled` é apenas campo de solicitação; resultados dependem do serviço.
- O solver executável nativo pertence ao projeto. Inspecione seu código-fonte e compile novamente se quiser reproduzibilidade de binário.
- Não interprete `max_continue_attempts=20` como limite de tokens nem como necessidade de escolher quantidade: é somente proteção.
- O dispositivo remoto identifica o cliente por cabeçalhos, mas **nenhum navegador é aberto**.

## Diagnóstico rápido

```powershell
python api_client.py --selftest
python -m unittest -q test_media test_auto_continue test_streaming test_credential_store
python -u api_client.py --stream --quiet "Responda OK."
```

Os dois primeiros comandos são offline. O último exige credencial e rede. Use status `FINISHED`, IDs e contagem de continuations para verificar a geração; não imprima tokens.
