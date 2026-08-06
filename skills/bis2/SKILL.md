---
name: bis2
description: Acessar servidores BIS2 por meio do executável BISCMD configurado localmente, incluindo validação de fachada EJB, consultas fiscais NFC-e, download de XML, revalidação de documento fiscal, envio de contingência offline, inutilização de numeração e atualização de status operacional. Use quando a tarefa mencionar BIS2, BISCMD, WildFly remoto do BIS, NFC-e no BIS ou operações fiscais disponíveis no BISCMD.
---

# Operar BIS2 via BISCMD

Usar exclusivamente `python skills/bis2/scripts/bis2.py`. O script lê
`data/config/bis2.toml`, obtém usuário e senha do KeePassXC e injeta credenciais no
processo do BISCMD sem gravá-las em arquivo temporário.

## Inicializar a configuração

```powershell
python scripts/integration_config.py init bis2
```

`profile` é o nome local de uma configuração de servidor BIS2, por exemplo `turing`.
Ele seleciona host, porta e referência de credencial. Quando houver mais de um
servidor, informar `--profile NOME`.

Operações com efeito externo ou alteração no BIS2 exigem `--profile` explícito mesmo
que exista `default_profile`.

## Executar

Usar `doctor` para validar Java, JAR, credencial e lookup das fachadas remotas:

```powershell
python skills/bis2/scripts/bis2.py --profile turing doctor
```

Consultas podem ser executadas dentro do escopo solicitado. Escritas exigem
autorização explícita e atual da pessoa usuária, incluindo confirmação dos alvos
operacionais.

Nunca chamar o JAR diretamente quando houver credenciais envolvidas. Não imprimir,
copiar ou persistir usuário/senha fora do cofre.

Ler [references/commands.md](references/commands.md) para comandos suportados,
exemplos e classificação de risco.
