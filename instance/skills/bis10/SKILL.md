---
name: bis10
description: Acessar instalações BIS10 por meio do BIS10CMD, incluindo diagnóstico, sessão e operações controladas de lançamentos financeiros e transferências. Use quando a tarefa mencionar BIS10, BIS10CMD, lançamentos financeiros ou transferências no BIS10.
---

# Operar BIS10 via BIS10CMD

Usar exclusivamente `python skills/bis10/scripts/bis10.py`. O script lê
`data/config/bis10.toml`, obtém as duas autenticações do KeePassXC e injeta os
valores somente no processo do cliente.

## Configuração conversacional

Inicialize o modelo privado:

```powershell
python scripts/integration_config.py init bis10
```

Cada perfil representa uma instalação/servidor BIS10 e contém host, porta, locale,
caminho do JAR, diretório de execução e duas referências de cofre:

- `BIS10/<Perfil>/ApplicationRealm`: usuário e senha do acesso EJB remoto;
- `BIS10/<Perfil>/BIS10`: usuário e senha para abrir a sessão BIS10.

As credenciais devem ser capturadas pelo broker protegido. Nunca editar senhas no
TOML, em argumentos ou no `application.properties` da instalação.

Para registrar o perfil depois da captura protegida:

```powershell
python scripts/integration_config.py profile add bis10 `
  --name local --host 127.0.0.1 --port 8080 `
  --jar-path C:/opt/BIS10CMD/BISCMD-10.0.jar `
  --working-dir C:/opt/BIS10CMD --locale pt-BR `
  --jndi-credential-ref BIS10/Local/ApplicationRealm `
  --bis-credential-ref BIS10/Local/BIS10
```

## Execução

Consultas e diagnóstico:

```powershell
python skills/bis10/scripts/bis10.py --profile local doctor
python skills/bis10/scripts/bis10.py --profile local ping
python skills/bis10/scripts/bis10.py --profile local session
```

Operações financeiras disponíveis:

```powershell
python skills/bis10/scripts/bis10.py --profile local account-create `
  --account-id 1 --category-id 10 --date 2026-08-08 --value 125.30 `
  --display-line "Despesa operacional" --confirm

python skills/bis10/scripts/bis10.py --profile local transfer-create `
  --debit-account-id 1 --credit-account-id 2 --date 2026-08-08 `
  --value 500.00 --confirm

python skills/bis10/scripts/bis10.py --profile local account-update `
  --id 123 --value 130.00 --confirm

python skills/bis10/scripts/bis10.py --profile local transfer-update `
  --id 200 --value 550.00 --confirm

python skills/bis10/scripts/bis10.py --profile local account-delete --id 123 --confirm
```

Toda escrita exige autorização explícita e atual, perfil explícito e `--confirm`.
O cliente não altera lançamentos do tipo `BILLS` diretamente.
