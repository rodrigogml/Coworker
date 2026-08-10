---
name: gmail
description: Buscar, ler e organizar mensagens, conversas, marcadores e rascunhos do Gmail por API, incluindo preparar e enviar rascunhos autorizados. Usar quando a tarefa mencionar Gmail, e-mail de uma conta Google, caixa de entrada, pesquisa de mensagens, threads, labels, rascunhos, lixeira ou envio pelo Gmail.
---

# Gerenciar Gmail

Usar `python skills/gmail/scripts/gmail.py`. Selecionar a conta com `--profile`
e obter OAuth internamente por `scripts/google_accounts.py`; nunca aceitar tokens em
argumentos ou respostas.

## Inicializar a configuração

Quando algum arquivo estiver ausente, inicializar os dois modelos sem sobrescrever:

```powershell
python scripts/integration_config.py init google
python scripts/integration_config.py init gmail
```

## Executar o fluxo

1. Usar `python scripts/google_accounts.py list` para conhecer os perfis locais.
2. Se a conta ainda não estiver autorizada ou não possuir Gmail, não pedir Client ID,
   Client Secret nem token. Apresentar uma lista numerada com Gmail, Agenda, Drive,
   Contatos e Todos; recomendar somente Gmail para uma solicitação de e-mail.
3. Após a escolha, executar `python scripts/google_accounts.py enroll --profile NOME
   --service gmail`, repetindo `--service` para os demais escolhidos, ou usar
   `--all-services`. Informar que o navegador deve abrir na máquina da instância; não
   enviar a URL a um celular remoto, pois o callback usa `127.0.0.1`.
4. Depois que a pessoa concluir o navegador, usar `doctor --profile NOME` para
   validar a conta selecionada.
5. Pesquisar com a sintaxe nativa do Gmail antes de abrir mensagens.
6. Ler somente os formatos necessários: preferir `metadata`, usar `full` quando o
   corpo for necessário e `raw` somente quando solicitado.
7. Preparar mensagens como rascunho a partir de arquivo RFC 822 UTF-8.
8. Enviar somente quando o pedido atual autorizar destinatários e conteúdo.
9. Tratar lixeira, marcadores e envio como alterações externas.

```powershell
python skills/gmail/scripts/gmail.py --profile pessoal doctor
python skills/gmail/scripts/gmail.py --profile pessoal messages list `
  --query "from:exemplo@empresa.com newer_than:30d"
python skills/gmail/scripts/gmail.py --profile pessoal messages show `
  --id ID --format full
python skills/gmail/scripts/gmail.py --profile pessoal threads show --id ID
python skills/gmail/scripts/gmail.py --profile pessoal labels list
```

## Preparar e enviar

O arquivo `.eml` deve conter cabeçalhos `From`, `To`, `Subject` e uma linha vazia antes
do corpo. A criação não envia a mensagem.

```powershell
python skills/gmail/scripts/gmail.py --profile pessoal drafts create `
  --message-file data/work/resposta.eml
python skills/gmail/scripts/gmail.py --profile pessoal drafts send --id ID
```

Não executar `drafts send` sem autorização explícita e atual. Após o envio, validar o
ID devolvido. Não oferecer exclusão permanente; usar a lixeira.

## Respeitar contratos

- Não improvisar endpoints ou chamadas arbitrárias.
- Não repetir automaticamente escritas após erro ou timeout.
- Não persistir access tokens; eles existem somente durante o processo.
- Usar `--all-pages` apenas quando necessário.
- Considerar que IDs de marcadores do sistema não são seus nomes visíveis.
- Ler [references/api-contracts.md](references/api-contracts.md) quando houver dúvida
  sobre OAuth, endpoints, escopos ou efeitos.
