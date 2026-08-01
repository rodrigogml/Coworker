# Scripts compartilhados

Este diretório contém utilitários usados por mais de uma skill.

## Memória SQLite

`memory.py` é a interface oficial de acesso ao banco `data/memory.sqlite3`.

Inicialização:

```powershell
python scripts/memory.py init
```

Registro:

```powershell
python scripts/memory.py remember `
  --kind preference `
  --subject "Comunicação" `
  --content "Prefere respostas em português." `
  --source "usuário" `
  --tag idioma
```

Pesquisa:

```powershell
python scripts/memory.py search "português"
python scripts/memory.py list --kind preference
python scripts/memory.py show mem_ID
```

Correção e exclusão:

```powershell
python scripts/memory.py supersede mem_ID `
  --source "usuário" `
  --content "Nova informação corrigida."

python scripts/memory.py forget mem_ID --confirm
```

Diagnóstico e backup:

```powershell
python scripts/memory.py status
python scripts/memory.py backup --output data/backups/memory-backup.sqlite3
```

Todos os comandos produzem JSON. Senhas, tokens, chaves privadas e outros segredos são
recusados. O parâmetro `--credential-ref` registra somente o nome de uma credencial
armazenada em um cofre externo.

## Cofre KeePassXC

`credential_vault.py` integra a Coworker ao KeePassXC conforme
`data/config/secrets.toml`.

Se a configuração local ainda não existir, copiar `config/secrets.example.toml` e
ajustar os caminhos antes de executar os comandos.

```powershell
python scripts/credential_vault.py status
python scripts/credential_vault.py create
python scripts/credential_vault.py enroll
python scripts/credential_vault.py open
python scripts/credential_vault.py add "APIs/Nome"
python scripts/credential_vault.py check "APIs/Nome"
```

O utilitário não possui comando para revelar credenciais. Criação e inclusão acontecem
em um console interativo separado, no qual o usuário digita informações confidenciais.

Integrações autenticadas por usuário e senha devem preferir uma única entrada:
`Username` guarda o identificador e `Password` guarda o segredo. Scripts podem importar
`read_entry_credentials` para obter o par internamente, sem imprimi-lo ou aceitá-lo em
argumentos.

`enroll` valida a senha mestra contra o cofre e a cadastra no Gerenciador de Credenciais
do Windows com persistência local à máquina. O cofre pode ser sincronizado pelo
provedor escolhido pela pessoa usuária; o cadastro local deve ser repetido em cada
computador.

## Entidades no cofre

`vault_entities.py` lê e grava os atributos personalizados definidos em
`config/vault-entities.toml`. A ferramenta exige PyKeePass e recusa gravações enquanto
a interface KeePassXC estiver aberta.

```powershell
python scripts/vault_entities.py inspect `
  --entry "Pessoas/Fisicas/Nome da Pessoa"

python scripts/vault_entities.py set `
  --entry "Pessoas/Fisicas/Nome da Pessoa" `
  --attribute CPF --prompt
```

`inspect` informa somente presença e proteção, nunca valores. Scripts de integração
devem importar `read_entry_attribute` e descartar o valor assim que a operação terminar.
Valores sensíveis entram por prompt ou entrada padrão e nunca por argumentos.
