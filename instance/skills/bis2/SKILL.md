---
name: bis2
description: Acessar servidores BIS2 por meio do executável BISCMD configurado localmente, incluindo validação de fachada EJB, consultas fiscais NFC-e, catálogo de itens, download de XML, revalidação de documento fiscal, envio de contingência offline, inutilização de numeração e atualização de status operacional. Use quando a tarefa mencionar BIS2, BISCMD, WildFly remoto do BIS, NFC-e, itens do cadastro ou operações fiscais disponíveis no BISCMD.
---

# Operar BIS2 via BISCMD

Usar exclusivamente `python skills/bis2/scripts/bis2.py`. O script lê
`data/config/bis2.toml`, obtém usuário e senha do KeePassXC e injeta credenciais no
processo do BISCMD sem gravá-las em arquivo temporário.

## Inicializar a configuração

```powershell
python scripts/integration_config.py init bis2
```

`profile` é o nome local de uma configuração de servidor BIS2, por exemplo `example`.
Ele seleciona host, porta e referência de credencial. Quando houver mais de um
servidor, informar `--profile NOME`.

Para criar um novo perfil privado sem editar TOML manualmente, usar o configurador
tipado:

```powershell
python scripts/integration_config.py profile add bis2 `
  --name local --host 127.0.0.1 --port 8080 `
  --credential-ref BIS2/Local/BISCMD
```

O comando escreve somente em `data/config/bis2.toml`, não sobrescreve um perfil
existente e não armazena credenciais. Depois valide com `bis2.py --profile local doctor`.

Operações com efeito externo ou alteração no BIS2 exigem `--profile` explícito mesmo
que exista `default_profile`.

## Executar

Usar `doctor` para validar Java, JAR, credencial e lookup das fachadas remotas:

```powershell
python skills/bis2/scripts/bis2.py --profile example doctor
```

Consultas podem ser executadas dentro do escopo solicitado. Escritas exigem
autorização explícita e atual da pessoa usuária, incluindo confirmação dos alvos
operacionais.

As consultas NFC-e aceitam exatamente todos os valores de
`DocFiscalVO.DocFiscalStatus` do BIS2: `SELLING`, `STORED`, `SOLD`, `CANCELLING`,
`CANCELED`, `ERROR`, `ERROR_SYNC`, `VOID`, `SEFAZVALIDATING`, `SEFAZPROBLEM` e
`SEFAZOFFLINE`. `SEFAZERROR` não existe nessa enumeração.

Nunca chamar o JAR diretamente quando houver credenciais envolvidas. Não imprimir,
copiar ou persistir usuário/senha fora do cofre.

Ler [references/commands.md](references/commands.md) para comandos suportados,
exemplos e classificação de risco.
## Recuperar NFC-e com problema de envio

Após corrigir os dados persistidos de uma NFC-e, o reparo do envio e a transmissão
são passos separados:

```powershell
python skills/bis2/scripts/bis2.py --profile example nfce-fix-envi-xml --doc-id 123 --confirm
python skills/bis2/scripts/bis2.py --profile example nfce-send-offline --doc-id 123 --confirm
```

`nfce-fix-envi-xml` chama `DocFiscalCrud.fixNFCeEnviXML(id)`, reconstrói o XML e
prepara o status `SEFAZOFFLINE`. `nfce-send-offline` chama o envio para a SEFAZ e
espera encontrar esse XML pronto nesse status. Não tratar os dois comandos como uma
única operação nem executar o envio antes da reconstrução.
