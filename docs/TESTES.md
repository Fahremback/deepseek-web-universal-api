# Testes, observações e reprodução

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
