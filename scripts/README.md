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

`credential_vault.py` integra a BOTina ao KeePassXC conforme
`data/config/secrets.toml`.

Se a configuração local ainda não existir, copiar `config/secrets.example.toml` e
ajustar os caminhos antes de executar os comandos.

```powershell
botina-secrets status
botina-secrets create
botina-secrets enroll
botina-secrets open
botina-secrets add "APIs/Nome"
botina-secrets check "APIs/Nome"
```

O utilitário não possui comando para revelar credenciais. Criação e inclusão acontecem
em um console interativo separado, no qual o usuário digita informações confidenciais.

`enroll` valida a senha mestra contra o cofre e a cadastra no Gerenciador de Credenciais
do Windows com persistência local à máquina. O cofre pode ser sincronizado pelo
provedor escolhido pela pessoa usuária; o cadastro local deve ser repetido em cada
computador.
