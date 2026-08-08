---
name: forwardemail
description: Gerenciar a conta Forward Email por API, incluindo consultar a conta, listar, verificar, criar, alterar e excluir domínios e listar, consultar, criar, alterar e excluir aliases. Use quando a tarefa mencionar Forward Email, encaminhamento de e-mail, aliases, destinatários, verificação de MX/TXT/SMTP ou proteções de domínio.
---

# Gerenciar Forward Email

Use exclusivamente `python skills/forwardemail/scripts/forward_email.py` para
acessar a API. O script obtém internamente a credencial referenciada em
`data/config/forwardemail.toml` do cofre KeePassXC e nunca a devolve ao agente.
Quando houver várias contas, selecionar a referência com `--profile NOME`.

## Inicializar a configuração

```powershell
python scripts/integration_config.py init forwardemail
```

O comando cria somente `data/config/forwardemail.toml` quando ele ainda não existe.

## Aplicar a autorização

- Executar consultas de leitura dentro do escopo pedido sem confirmação adicional.
- Executar uma alteração quando o pedido atual autorizar explicitamente a operação e
  identificar exatamente domínio, alias, destinatários e valores.
- Pedir os dados ausentes quando o alvo ou o estado desejado forem ambíguos.
- Exigir autorização explícita e atual para excluir um domínio ou alias.
- Usar `--dry-run` quando for útil como prévia técnica. Não tratar a prévia como
  autorização.
- Não enviar mensagens e não gerar senhas de caixas postais. Esses fluxos não fazem
  parte desta versão da skill e exigem desenho específico antes de serem adicionados.

## Executar o fluxo

1. Usar `python skills/forwardemail/scripts/forward_email.py doctor` quando
   for necessário diagnosticar autenticação.
2. Consultar domínios ou aliases para resolver o alvo e verificar o estado atual.
3. Recusar seleção ambígua; preferir `--alias-id` quando nomes não forem suficientes.
4. Executar a alteração já autorizada, sem confirmação artificial adicional.
5. Examinar o JSON e informar se houve mudança, estado já satisfatório, simulação ou
   falha.

Não usar `curl`, não consultar diretamente o KeePassXC e não imprimir variáveis de
ambiente ou cabeçalhos de autenticação.

## Comandos

```powershell
python skills/forwardemail/scripts/forward_email.py doctor
python skills/forwardemail/scripts/forward_email.py account show

python skills/forwardemail/scripts/forward_email.py domains list
python skills/forwardemail/scripts/forward_email.py domains show `
  --domain exemplo.com
python skills/forwardemail/scripts/forward_email.py domains verify `
  --domain exemplo.com
python skills/forwardemail/scripts/forward_email.py domains create `
  --domain exemplo.com
python skills/forwardemail/scripts/forward_email.py domains update `
  --domain exemplo.com --phishing-protection
python skills/forwardemail/scripts/forward_email.py domains delete `
  --domain exemplo.com

python skills/forwardemail/scripts/forward_email.py aliases list `
  --domain exemplo.com
python skills/forwardemail/scripts/forward_email.py aliases show `
  --domain exemplo.com --name contato
python skills/forwardemail/scripts/forward_email.py aliases create `
  --domain exemplo.com --name contato `
  --recipient destino@exemplo.net
python skills/forwardemail/scripts/forward_email.py aliases update `
  --domain exemplo.com --name contato `
  --recipient novo-destino@exemplo.net
python skills/forwardemail/scripts/forward_email.py aliases delete `
  --domain exemplo.com --name contato
```

Acrescentar `--dry-run` aos comandos de criação, alteração ou exclusão para validar e
mostrar a operação sem enviá-la. Usar opções booleanas positivas ou negativas, por
exemplo `--phishing-protection` e `--no-phishing-protection`.

Ao criar um domínio, a skill não cria um alias curinga implicitamente: destinatários
de catch-all precisam ser fornecidos por `--catchall-recipient`. Ao criar um alias, ao
menos um `--recipient` é obrigatório.

## Interpretar limites

- A conta e o plano determinam quais recursos podem ser ativados.
- A verificação consulta os endpoints de registros DNS e SMTP e depois atualiza a
  leitura do domínio.
- A criação idêntica de domínio ou alias retorna `changed: false` quando o estado
  existente pode ser reconhecido com segurança.
- Consultar [references/api-contracts.md](references/api-contracts.md) para endpoints,
  autenticação, paginação e recursos deliberadamente excluídos.
