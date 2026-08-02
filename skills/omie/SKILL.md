---
name: omie
description: Consultar e manipular dados permitidos do ERP Omie por API, incluindo clientes e fornecedores, projetos, categorias, departamentos, contas correntes, contas a pagar e receber, baixas, conciliações e transferências entre contas. Use quando a tarefa mencionar Omie, ERP, contrapartes, projetos ou operações financeiras mantidas no Omie.
---

# Operar o ERP Omie

Usar exclusivamente `python skills/omie/scripts/omie.py`. O script obtém App Key e
App Secret internamente de uma única entrada do KeePassXC e nunca os aceita em
argumentos ou arquivos de entrada.

## Preparar a integração

Inicializar somente quando a configuração privada estiver ausente:

```powershell
python scripts/integration_config.py init omie
```

Quando houver mais de uma empresa, informar sempre `--profile NOME`. Toda escrita
exige o perfil explícito, mesmo que exista `default_profile`.

## Respeitar o escopo

- Consultar sem confirmação dentro do pedido atual.
- Exigir autorização explícita e atual antes de criar, alterar, inativar, excluir,
  baixar, cancelar, conciliar, desconciliar ou transferir.
- Não tratar `--dry-run` como autorização; ele é somente uma prévia técnica.
- Nunca improvisar `call`, endpoint ou campo fora da allowlist do script.
- Não usar `Upsert`; criações usam `request_id` estável e código de integração
  determinístico.
- Manter boleto, PIX, faturamento e emissão fiscal fora desta skill.

## Consultar

Usar `doctor` quando for necessário diagnosticar autenticação. Preferir `show`
com identificador inequívoco e limitar listagens antes de usar `--all-pages`.

```powershell
python skills/omie/scripts/omie.py --profile EMPRESA doctor
python skills/omie/scripts/omie.py --profile EMPRESA customers show --id 123
python skills/omie/scripts/omie.py --profile EMPRESA projects list
python skills/omie/scripts/omie.py --profile EMPRESA categories list
python skills/omie/scripts/omie.py --profile EMPRESA departments show --code DEP-1
python skills/omie/scripts/omie.py --profile EMPRESA current-accounts list
python skills/omie/scripts/omie.py --profile EMPRESA transfers show --integration-id ID
```

Examinar `pagination.truncated` e continuar por `--page` quando necessário.

## Alterar

Fornecer um envelope JSON versão 1 por arquivo UTF-8 ou stdin, nunca por flags de
campos. Colocar `--profile` antes do recurso:

```powershell
python skills/omie/scripts/omie.py --profile EMPRESA projects create `
  --input-file entrada.json --dry-run

Get-Content entrada.json -Raw | python skills/omie/scripts/omie.py `
  --profile EMPRESA payables create --input-stdin
```

Usar o mesmo `request_id` ao retomar uma operação. Em lote, corrigir a falha e
reenviar o mesmo envelope: a skill valida todos os itens antes da primeira escrita,
executa em sequência e reconhece criações já existentes.

Inativar clientes, fornecedores e projetos antes da exclusão física. Toda exclusão
exige `confirm_delete: true`. Não excluir título com baixa ativa.

Se o resultado for `unknown`, consultar pelo identificador de integração antes de
repetir. A skill tenta essa recuperação automaticamente nas criações.

Ler [references/operations.md](references/operations.md) para envelopes, campos e
exemplos de escrita. Ler [references/resources.md](references/resources.md) para
seletores e consultas, e [references/api-contracts.md](references/api-contracts.md)
para endpoints, allowlist, limites e erros.
