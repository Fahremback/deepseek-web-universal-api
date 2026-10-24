# Segurança, erros, operação e limites

## Segurança de credenciais

1. O bearer é um segredo da conta. Nunca o inclua em código, relatório, captura, repositório, URL ou log.
2. `credential_store.py` usa `keyring` para gravar token, device id, prova `hif_leim` e cookies de sessão autorizados (`ds_session_id`, `HWWAFSESID`, `HWWAFSESTIME`), quando presentes.
3. `import_existing_session.py` lê local storage offline **somente** dos perfis padrão Edge/Chrome e origem `https://chat.deepseek.com`, valida o token e então grava no cofre. Não abre navegador nem cria autenticação nova.
4. Para excluir a credencial: `python api_client.py --forget-credentials`. A exclusão não invalida token remoto; a revogação é responsabilidade do provedor/conta.
5. `DEEPSEEK_TOKEN`, `DEEPSEEK_TOKEN_FILE`, `--token`, `DEEPSEEK_DEVICE_ID`, `DEEPSEEK_COOKIES`, `DEEPSEEK_HIF_LEIM`, `DEEPSEEK_LOCALE` e `DEEPSEEK_TIMEZONE_OFFSET` permitem provisionamento/configuração explícita. Evite token na linha de comando compartilhada por histórico de terminal.

## Tratamento de falhas

| Condição | Comportamento implementado |
| --- | --- |
| Token ausente | `AuthenticationError` |
| HTTP 401/403 | Tenta recarregar credencial; sem sucesso, `AuthenticationError` |
| Resposta JSON com `code`/`biz_code` não zero | `DeepSeekAPIError` com dados de diagnóstico |
| Conteúdo não SSE no endpoint de geração | Analisa erro JSON e lança exceção |
| Algoritmo PoW inesperado, dados inválidos ou nonce não encontrado | `ProofOfWorkError` |
| SSE fecha sem evento `close` | `DeepSeekAPIError` |
| `INCOMPLETE` além do teto de tentativas | `DeepSeekAPIError` |
| Upload ou processamento de arquivo falhou, ou expirou o tempo | `DeepSeekAPIError` |
| Falha de conexão ou timeout | Exceção do `requests` propagada à CLI |

## Cuidados de operação

- A interface web privada pode alterar cabeçalhos, campos, PoW, controles WAF, limites ou semântica sem aviso.
- Não há garantia de estabilidade, SLA, limites de taxa fixos, contagem oficial de tokens ou autenticação desassistida inicial.
- Um token inicialmente válido pode expirar/revogar. Sem credencial substituta, o cliente não acessa a conta; não há bypass de autenticação.
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
