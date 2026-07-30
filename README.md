# BOTina

BOTina é uma assistente pessoal local, de identidade feminina, orientada por arquivos
de instruções e capaz de executar ferramentas de linha de comando com segurança.

O repositório contém apenas o núcleo reutilizável: instruções, skills, scripts,
migrations, testes e modelos. Memória, configurações locais, bancos e credenciais
permanecem em `data/`, fora do Git.

> [!IMPORTANT]
> O projeto está em estágio experimental e é **Windows-first**. Memória, instruções e
> integrações baseadas em Python são portáveis, mas o desbloqueio automático do cofre
> atualmente depende do Gerenciador de Credenciais do Windows. Provedores para macOS e
> Linux poderão ser adicionados no futuro.

## Capacidades atuais

- memória legível por humanos em Markdown;
- memória estruturada em SQLite;
- cofre KeePassXC sem exposição de segredos ao agente;
- gerenciamento de zonas e registros DNS da Cloudflare;
- base para novas skills pessoais.

## Estrutura

```text
BOTina/
├── AGENTS.md
├── README.md
├── .gitignore
├── .agents/
├── config/
│   ├── cloudflare.example.toml
│   ├── memory-policy.yaml
│   └── secrets.example.toml
├── data/                         # privado e ignorado pelo Git
│   ├── config/
│   ├── memory/
│   ├── secrets/
│   └── memory.sqlite3
├── migrations/
├── scripts/
├── skills/
├── templates/
│   └── memory/
└── tests/
```

## Dependências

- Windows 10 ou 11 para a integração atual com o Gerenciador de Credenciais;
- Python 3.11 ou superior disponível como `python`;
- KeePassXC e `keepassxc-cli` para o cofre;
- Git apenas para versionar o núcleo público;
- acesso à internet somente para integrações que precisem de APIs.

SQLite já faz parte do Python; não é necessário instalar um executável separado.
As ferramentas atuais usam somente a biblioteca padrão do Python e não exigem
`pip install`.

Antes do bootstrap, a IA deve diagnosticar o ambiente:

```powershell
Get-Command python -ErrorAction SilentlyContinue
python --version
Get-Command git -ErrorAction SilentlyContinue
```

Se Python 3.11 ou superior não estiver disponível, a IA pode instalar somente quando
estiver autorizada. Caso não possa, deve orientar a instalação pelo
[site oficial do Python](https://www.python.org/downloads/windows/), pedir que a opção
de adicionar o Python ao `PATH` seja habilitada e retomar pelo comando
`python --version`.

Git não é necessário para executar a BOTina. Se a tarefa exigir versionamento e ele
estiver ausente, aplicar a mesma regra: instalar quando autorizado ou orientar a
instalação pelo [site oficial do Git](https://git-scm.com/download/win).

## Bootstrap da instância local

Uma IA deve executar estas etapas de forma idempotente. Arquivos existentes nunca
devem ser sobrescritos.

### 1. Criar a área privada

```powershell
New-Item -ItemType Directory -Force data/config, data/memory, data/secrets | Out-Null

$modelos = @{
  "config/secrets.example.toml" = "data/config/secrets.toml"
  "config/cloudflare.example.toml" = "data/config/cloudflare.toml"
  "templates/memory/README.md" = "data/memory/README.md"
  "templates/memory/profile.md" = "data/memory/profile.md"
  "templates/memory/preferences.md" = "data/memory/preferences.md"
}

foreach ($origem in $modelos.Keys) {
  $destino = $modelos[$origem]
  if (-not (Test-Path -LiteralPath $destino)) {
    Copy-Item -LiteralPath $origem -Destination $destino
  }
}
```

Esse bloco pula cada arquivo que já existir.

### 2. Ajustar a configuração local

Editar `data/config/secrets.toml` com os caminhos reais do KeePassXC, do cofre e o
identificador local do Gerenciador de Credenciais. Esses valores não são senhas, mas
descrevem a instalação pessoal e permanecem fora do Git.

Editar `data/config/cloudflare.toml` apenas quando a referência da credencial ou o
endpoint forem diferentes do modelo. Nunca gravar o token nesse arquivo.

### 3. Inicializar a memória

```powershell
python scripts/memory.py init
python scripts/memory.py status
```

### 4. Preparar o KeePassXC

```powershell
python scripts/credential_vault.py status
python scripts/credential_vault.py create
python scripts/credential_vault.py enroll
```

`create` somente deve ser usado quando o cofre ainda não existir. `enroll` abre um
console separado para a pessoa usuária digitar a senha mestra; ela nunca deve ser
pedida na conversa.

Quando o KeePassXC não estiver disponível, a IA deve:

1. procurar primeiro os caminhos configurados e os comandos no `PATH`;
2. informar exatamente qual executável não foi encontrado;
3. instalar ou baixar somente quando o pedido atual autorizar essa ação;
4. usar a [fonte oficial do KeePassXC](https://keepassxc.org/download/) e validar a
   instalação;
5. quando não puder instalar, fornecer passos objetivos e aguardar a pessoa usuária.

### 5. Criar comandos convenientes

Em um diretório já incluído no `PATH`, podem ser criados wrappers como:

```batch
@echo off
python "CAMINHO_DO_PROJETO\scripts\credential_vault.py" %*
```

Salvar esse wrapper como `botina-secrets.cmd`. A skill da Cloudflare pode receber um
wrapper equivalente chamado `cloudflare.cmd`.

### 6. Validar integrações

```powershell
botina-secrets status
botina-secrets check "APIs/CloudFlare"
cloudflare doctor
```

Se a entrada da Cloudflare não existir, a IA deve orientar:

```powershell
botina-secrets add "APIs/CloudFlare"
```

O token deve ser digitado exclusivamente na janela confidencial aberta pelo comando.

## Como agir quando algo estiver ausente

A IA deve diferenciar três situações:

- **Arquivo local ausente:** criar diretórios e copiar o modelo correspondente.
- **Dependência ausente:** detectar caminhos e versões; instalar somente com
  autorização, ou orientar a instalação manual.
- **Interação confidencial ou login:** abrir a interface apropriada e solicitar que a
  pessoa usuária conclua a etapa fora da conversa.

Não inventar valores pessoais, não substituir arquivos locais e não guardar segredo em
Markdown, TOML, SQLite, logs ou argumentos de linha de comando.

## Memória

Arquivos Markdown privados ficam em `data/memory/`. O formato recomendado é:

```markdown
### Título objetivo

- Tipo: fato informado | inferência | decisão
- Origem: usuário | arquivo | sistema
- Atualizado em: YYYY-MM-DD
- Escopo: global | projeto | tarefa

Conteúdo da memória.
```

A memória SQLite usa `data/memory.sqlite3`:

```powershell
python scripts/memory.py remember `
  --kind preference `
  --subject "Comunicação" `
  --content "Prefere respostas em português." `
  --source "usuário"

python scripts/memory.py search "português"
```

Senhas e tokens são recusados. Apenas referências externas podem ser registradas com
`--credential-ref`.

## Cloudflare

```powershell
cloudflare zones list
cloudflare dns list --zone exemplo.com
cloudflare dns proxy --zone exemplo.com --name www --type A --enable
```

Consultas podem ser feitas diretamente dentro do escopo solicitado. Alterações exigem
autorização explícita na conversa. `--dry-run` é uma prévia técnica, não uma
confirmação.

## Privacidade e publicação

O conteúdo de `data/` é pessoal, ainda que algum arquivo isolado pareça inofensivo.
Nunca usar `git add --force` nessa pasta. Antes de cada publicação:

```powershell
git status --short --ignored
git diff --cached
```

Revise também o histórico e execute uma ferramenta de detecção de segredos. Cofres
KeePassXC, bancos SQLite, `.env`, chaves e configurações locais são ignorados.

## Testes

```powershell
python -m unittest discover -s tests -v
```

O workflow público executa esses testes no GitHub Actions com Windows e Python 3.11 e
3.12. Ele não recebe acesso ao conteúdo local de `data/`, ao cofre ou a credenciais.
