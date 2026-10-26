# Auditoria offline do cliente HTTP (25/10/2026)

## Escopo

O cliente já realiza requisições HTTP sem Chromium no fluxo de geração.
Esta auditoria **não executa autenticação**, não tenta contornar suspensões e
**não entra em contato com o DeepSeek**. Compara capturas existentes com os
endereços de protocolo conhecidos no projeto. O código da API reversa não foi
alterado nesta etapa.

## Como reproduzir

Na pasta `deepseek_auth_20261024`:

```powershell
python protocol_audit.py ..\..\har\recording.har ..\..\har\8197eaeab4ff\recording.har --output docs\PROTOCOLO_AUDIT_20261025.json
python -m unittest discover -p "test_*.py" -q
```

## Evidências e limites

| Fonte | Dados observados | O que prova |
| --- | --- | --- |
| HAR de conversação, 24/10 | Três HTTP 200 de PoW, criação de sessão e conclusão | Essas chamadas foram atendidas naquela captura |
| HAR da interface, 24/10 | 64 entradas totais, 29 vinculadas a domínios DeepSeek, incluindo configuração da aplicação | O navegador realiza atividades além do fluxo de conversação isolado |
| Cliente HTTP atual | Login, PoW, criação de conversa, stream e upload implementados | Existe implementação dessas etapas, mas não um teste de autenticação atual |
| Testes locais | Mock de backend e simulações de SSE/continuação | A lógica funciona contra as condições simuladas |

**Não está comprovado:** que requisições de configuração adicionais sejam
exigidas por cada sessão; que reproduzi-las alteraria decisões antifraude; que
o serviço aceite esta integração após a suspensão ou que a API web gratuita
possa ser usada indefinidamente para workloads automatizados.

Um status 200 no HAR não prova autorização futura. Uma conta suspensa impede
uma validação real legítima com aquela conta, então a auditoria é offline.

## Próximas condições de validação

1. Obter esclarecimento do DeepSeek quanto ao bloqueio e ao uso automatizado.
2. Com acesso permitido, testar uma única autenticação real, sem automatizar
   CAPTCHA ou contornar controles de segurança, e verificar resposta de sessão.
3. Comparar o resultado autorizado com as hipóteses deste relatório sem expor
   cookies ou tokens.
4. Verificar streaming, interrupção, limites e estado recuperável end-to-end.

O protocolo proprietário pode mudar; não existe garantia de não suspensão.

## Tratamento de dados

`protocol_audit.py` gera apenas métodos, caminhos estáticos previamente
conhecidos, status, contagens e horários. Omitirá token, cookie, payload,
parâmetro de consulta, endereço de conta e URL não reconhecida. Não envie
arquivos HAR brutos para suporte público.
