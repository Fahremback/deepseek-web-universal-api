# Documentação - DeepSeek Web HTTP Client

Cliente independente de protocolo privado observado em chat.deepseek.com. **Não é a API oficial da DeepSeek nem um servidor OpenAI-compatible.** A implementação fornece biblioteca Python e CLI, sem abrir navegador.

## Índice

1. [Instalação e início rápido](INSTALACAO.md)
2. [Referência Python e CLI](REFERENCIA.md)
3. [Protocolo HTTP e prova de trabalho](PROTOCOLO.md)
4. [Mídias e arquivos](MIDIAS.md)
5. [API para harnesses OpenAI e Anthropic](HARNESSES.md)
6. [Streaming e continuidade](STREAMING.md)
7. [Segurança, erros e limites](SEGURANCA.md)
8. [Testes e evidências](TESTES.md)
9. [Paper técnico](../paper/PAPER.md) ([PDF](../paper/PAPER.pdf))

## Código

- `api_client.py`: autenticação, HTTP, DeepSeekHashV1, upload e processamento de arquivos, SSE, continuidade e interface de linha de comando.
- `openai_server.py`: servidor HTTP local compatível com OpenAI Chat Completions, Responses e Anthropic Messages.
- `harness_adapter.py`, `anthropic_adapter.py`: serialização de mensagens, anexos, ferramentas e validação de argumentos.
- `credential_store.py`: leitura/gravação de credenciais via keyring do sistema.
- `import_existing_session.py`: importação offline e opcional de sessão de perfil local já existente, sem inicializar navegador.
- `ds_pow_solver.c` e `ds_pow_solver.exe`: acelerador C do PoW; fallback puro em Python.
- `test_*.py`: testes automatizados. `examples/`: exemplos executáveis.

## Duas continuidades diferentes

- Geração com `INCOMPLETE`: continuação *da mesma resposta* via `POST /api/v0/chat/continue`; automática por padrão.
- Próximo turno em sessão existente: informar `chat_session_id` e `parent_message_id` ao enviar *nova mensagem*. O servidor mantém o contexto.

Estado em 24/10/2026. Uploads de imagem PNG e documento PDF foram validados por HTTP contra o servidor; outros formatos dependem das capacidades efetivas da conta e do serviço. Protocolo não oficial sujeito a alterações sem aviso.
