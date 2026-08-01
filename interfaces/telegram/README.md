# Interface Telegram

As respostas do Codex são normalizadas na saída: Markdown comum é convertido para o
subconjunto HTML aceito pelo Telegram, com escape obrigatório do conteúdo. Títulos,
ênfases, listas, links, citações e código preservam formatação compatível; links para
caminhos locais são apresentados como texto, pois não são acessíveis pelo aplicativo.

Esta aplicação conecta uma conversa particular do Telegram a sessões não interativas
do Codex CLI. Ela é uma interface da Coworker, não uma skill: recebe mensagens e mídias,
controla autorização, mantém uma fila local e devolve a resposta final do Codex.

## Limites atuais

- somente uma pessoa proprietária;
- somente conversa particular;
- uma execução Codex por vez;
- long polling como transporte operacional;
- textos, documentos, fotos, áudio, voz e vídeo;
- respostas nativas, mensagens referenciadas e álbuns de entrada;
- artefatos de saída como foto, documento, áudio, voz, vídeo ou animação;
- webhook reservado na configuração, ainda sem servidor ativado;
- uma única máquina deve executar o bot por vez.

O token, a pessoa autorizada, o estado, as mensagens e os arquivos recebidos são dados
locais e não são versionados. O modelo público contém apenas nomes de campos e valores
seguros.

## Preparação

1. Execute o instalador da raiz. Ele coleta a identidade e cria a configuração sem
   sobrescrever arquivos existentes:

   ```powershell
   .\install.ps1
   ```

2. Crie um bot usando o BotFather e mantenha desabilitada sua participação em grupos.
   O `@username` é definido no BotFather e pode ser diferente do nome da instância.
3. O instalador cadastra o token na referência específica da instância, com o formato:

   ```text
   APIs/Telegram/<instance_id>
   ```

4. O próprio instalador publica nome, descrições e comandos, abre o pareamento,
   apresenta os IDs numéricos para confirmação local e inicia o gateway. A vinculação
   continua exigindo confirmação humana na máquina; validar o PIN no Telegram não é
   suficiente.

5. No menu do configurador, abra **Codex CLI, CODEX_HOME e permissões**. O Codex CLI é
   obrigatório: a seção informa se o executável foi localizado, mostra sua versão,
   cria o `CODEX_HOME` exclusivo da instância e permite autenticar a conta isolada.
   O executável interno do aplicativo desktop não deve ser presumido como acessível a
   processos externos.
6. Se o comando `codex` não estiver no `PATH`, informe o caminho comprovado nessa
   seção. O valor é registrado em `codex.executable`.
7. O campo `codex.home_dir` identifica a instância isolada usada pelo gateway. O
   configurador grava explicitamente o padrão
   `%LOCALAPPDATA%\Coworker\instances\<instance_id>\codex`; ele não deve apontar para o
   diretório do Codex Desktop.
8. Mantenha `codex.network_access = false` até decidir habilitar integrações externas.
   Para Gmail, Notion e outras APIs, altere-o para `true` após autorização explícita.
9. Execute o diagnóstico:

   ```powershell
   python interfaces/telegram/gateway.py doctor
   ```

O instalador usa o App Server como backend inicial, mantendo `exec` disponível como
fallback. Para selecionar explicitamente o fallback, altere somente o arquivo privado:

```toml
[codex]
backend = "exec"
```

Depois reinicie o processo e execute `doctor`. A interface não troca esse valor nem
reescreve configurações privadas. Ambos os backends usam o mesmo schema de entrega;
`exec` usa `--output-schema`, enquanto o App Server usa `turn/start.outputSchema`.

O gateway mantém as regras de execução do `config/codex.rules` sincronizadas
em `<codex.home_dir>/rules/gateway.rules`. Elas liberam comandos de leitura e os pontos
de entrada públicos das integrações, mantendo comandos arbitrários sujeitos à política
do Codex. A sincronização ocorre ao iniciar o polling e também pode ser administrada
localmente:

```powershell
python interfaces/telegram/gateway.py permissions status
python interfaces/telegram/gateway.py permissions sync
```

Ao iniciar o polling, a aplicação publica automaticamente no Telegram o menu de
comandos válidos, o nome e as descrições derivados de `data/config/identity.toml`.
O username não pode ser alterado pela Bot API. Para consultar ou reaplicar essas
configurações sem reiniciar o gateway, use:

```powershell
python interfaces/telegram/gateway.py commands status
python interfaces/telegram/gateway.py commands sync
python interfaces/telegram/gateway.py profile status
python interfaces/telegram/gateway.py profile sync
```

A lista publicada e a resposta de `/help` usam a mesma definição no código. Assim,
uma instalação nova precisa apenas ter o token cadastrado antes da primeira execução.

## Vinculação inicial

Abra o polling em um terminal:

```powershell
python interfaces/telegram/gateway.py run
```

O configurador local também administra o processo persistente. Execute
`python scripts/install_instance.py` e escolha **6. Gerenciar gateway Telegram**
para consultar o status, iniciar, finalizar ou reiniciar. O processo registra PID e
horário em `<state_dir>/gateway-runtime.json`; a finalização usa uma
solicitação cooperativa e espera o polling corrente terminar. O registro
também impede duas cópias gerenciadas da mesma instância.

As opções de instalar e remover como serviço já aparecem reservadas no menu,
mas ainda não executam alterações no Windows. Um gateway iniciado antes desta
versão não possui registro persistente e deve ser finalizado manualmente uma vez
antes de passar ao novo gerenciador.

Em outro terminal, gere um PIN temporário:

```powershell
python interfaces/telegram/gateway.py pairing begin
```

Envie `/pair 123456` na conversa particular, usando o PIN realmente apresentado. A
mensagem valida o PIN, mas ainda não autoriza a conta. Consulte localmente a solicitação:

```powershell
python interfaces/telegram/gateway.py pairing status
```

Confira nome, username, `user_id` e `chat_id`. Somente então use o `approval_code`:

```powershell
python interfaces/telegram/gateway.py pairing approve ABC123
```

O PIN possui seis dígitos, é armazenado somente como PBKDF2-HMAC, expira, aceita um
número limitado de tentativas e é invalidado depois do uso. A aprovação local cria a
raiz de confiança da instalação.

## Conversa

- `/new`: desvincula a sessão atual; não apaga o histórico do Codex;
- `/resume`: quando enviado como resposta a uma mensagem anterior da Coworker, torna a
  thread daquela mensagem explicitamente ativa;
- `/status`: mostra sessão e fila;
- `/usage`: consulta as janelas de franquia da conta autenticada no Codex;
- `/cancel`: solicita o encerramento da execução ativa;
- `/thread`: mostra o identificador da sessão;
- `/secret NomeDoServico`: prepara a captura protegida da próxima mensagem de texto;
- `/help`: lista os comandos.

`/secret` não exige conhecer a estrutura do KeePassXC. Por exemplo, `/secret Todoist`
usa a entrada `APIs/Todoist`. A mensagem seguinte é interceptada antes de ser gravada
no SQLite, incluída em um job ou enviada ao Codex. O gateway salva o valor no campo
`Password`, registra somente `[Censurado por segurança]` e solicita `deleteMessage` à
Bot API. Se o Telegram não permitir a exclusão, a resposta orienta apagar a mensagem
manualmente sem repetir o segredo. `/cancel` abandona uma captura pendente.

Responder a uma mensagem inclui um único nível de contexto, quote e anexos conhecidos,
sem trocar a thread ativa. A confirmação, a resposta final e o primeiro artefato usam
`reply_parameters` para apontar nativamente à solicitação. IDs devolvidos pelo Telegram,
thread e turn são persistidos no SQLite.

No backend `exec`, o gateway usa `codex exec --json` e `codex exec resume`. No backend
`app-server`, inicia um transporte stdio, faz `initialize`/`initialized`, cria ou retoma
a thread, usa `localImage` para imagens e encerra somente em `turn/completed`. Apenas o
`agentMessage` final é publicado; raciocínio, logs, comandos e saída de ferramentas não
são retransmitidos. `/cancel` termina o processo `exec` ou envia `turn/interrupt` ao
App Server.

## Trabalhos e artefatos

Cada solicitação cria uma caixa isolada em `data/telegram/jobs/<job-id>/`:

```text
input/                 arquivos atuais e referenciados
derived/               conteúdo preparado pelos processadores
output/                únicos arquivos elegíveis para envio
delivery-schema.json   contrato fornecido ao Codex
result.json            resposta estruturada original
```

Um artefato declarado precisa ser relativo a `output/`, regular, não vazio, estar sob
o limite de upload e permanecer dentro da pasta após `Path.resolve(strict=True)`.
Links, junctions e reparse points que escapem da saída são rejeitados. O gateway
detecta MIME por assinatura quando possível, calcula SHA-256 e escolhe o método nativo:
JPEG/WebP e PNG sem alpha como foto; PNG com alpha e arquivos genéricos como documento;
GIF como animação; áudio convencional como áudio; OGG como voz; vídeo como vídeo.

O publicador `interfaces/telegram/scripts/publish_artifact.py` é o único comando de
cópia liberado ao Codex. Ele recebe o destino somente por `COWORKER_JOB_OUTPUT`, não
sobrescreve arquivos e devolve apenas metadados seguros. Arquivos em
`<CODEX_HOME>/generated_images` são concedidos somente para leitura.

## Processadores

Sem dependências externas, a interface prepara texto UTF-8, JSON, CSV, XML, inventário
de ZIP e extração básica de DOCX/XLSX por XML interno. ZIP possui limites de membros e
tamanho descompactado e rejeita path traversal. Imagens seguem como entrada visual
nativa. PDF, OCR, áudio e vídeo possuem diagnóstico opcional; quando a dependência não
está disponível, o original é preservado e essa limitação é informada ao Codex. A
interface nunca instala processadores automaticamente nem executa arquivos recebidos.

Os limites ficam em `[processors]` e `[media]` no TOML privado. A retenção dos jobs é
manual nesta versão: remova caixas antigas somente com o gateway parado e depois de
confirmar que seus artefatos não são mais necessários.

`/usage` usa `account/rateLimits/read` do Codex App Server. A resposta pode incluir
limites primário e secundário, percentuais disponíveis, horários de renovação, plano
e créditos adicionais quando esses campos forem fornecidos pela conta. Essa consulta
não cria uma sessão Codex e não consome uma solicitação ao modelo.

Cada processo recebe `CODEX_HOME` exclusivamente pelo ambiente filho. Assim, login,
configuração, sessões, logs e estado do gateway permanecem separados do Codex Desktop
sem alterar variáveis globais do Windows.

## Dados e segurança

Por padrão, o SQLite operacional fica em
`%LOCALAPPDATA%\Coworker\instances\<instance_id>\telegram`. Isso evita
sincronização concorrente de um banco ativo. Entradas, derivados e saídas ficam em
`data/telegram/jobs/`, com metadados, tamanho e SHA-256, e permanecem ignorados pelo Git.

A autorização usa os IDs numéricos do usuário e da conversa, nunca o `@username`.
Mensagens de grupos, usuários não autorizados e tipos desconhecidos não chegam ao
Codex. Arquivos recebidos são tratados como conteúdo não confiável e nunca são
executados pelo gateway.

Use `workspace-write` como sandbox inicial. O gateway converte essa opção em um perfil
de permissões explícito do Codex: o projeto é somente leitura e apenas `data/` recebe
escrita. O campo
`codex.network_access` controla separadamente a rede dos comandos executados. Libere
outros diretórios para leitura em `codex.additional_directories` ou, deliberadamente,
para escrita em `codex.writable_directories`; não use
`danger-full-access` para compensar uma configuração incompleta.

Na configura&ccedil;&atilde;o interativa, as listas de leitura e escrita s&atilde;o
gerenciadas incrementalmente: novos caminhos podem ser informados separados por
v&iacute;rgula, e cada item pode ser removido individualmente. O separador antigo por
ponto e v&iacute;rgula continua aceito. Caminhos relativos s&atilde;o resolvidos a partir
da raiz do RodriClone; `.` representa o pr&oacute;prio reposit&oacute;rio.

Para escrita, o menu oferece dois atalhos seguros: manter somente `data/`, que
continua sendo o padr&atilde;o de novas inst&acirc;ncias, ou adicionar `.` para permitir
que uma inst&acirc;ncia altere seu pr&oacute;prio reposit&oacute;rio. Outros caminhos formam o
perfil personalizado. A permiss&atilde;o de arquivos n&atilde;o libera comandos por si s&oacute;:
os pontos de entrada necess&aacute;rios ainda devem existir em `config/codex.rules`.


Quando a pessoa propriet&aacute;ria realmente precisar de uma inst&acirc;ncia com acesso
global, use a op&ccedil;&atilde;o **10. Super inst&acirc;ncia** no configurador local. A
ativa&ccedil;&atilde;o exige duas confirma&ccedil;&otilde;es e grava
`codex.access_mode = "super"`. Esse perfil for&ccedil;a `danger-full-access`, rede
habilitada e `approval_policy = "never"`; tamb&eacute;m remove somente
`<codex.home_dir>/rules/gateway.rules`, que &eacute; o arquivo restritivo gerado pelo
gateway. Outras regras mantidas pela pessoa propriet&aacute;ria n&atilde;o s&atilde;o
apagadas.

O modo super executa com todas as permiss&otilde;es da conta do Windows que iniciou o
gateway. Ele pode ler, alterar ou excluir dados e executar Git, PowerShell, Python e
outros aplicativos dispon&iacute;veis. A ativa&ccedil;&atilde;o nunca pode ser feita por
um comando do Telegram. Reinicie o gateway ap&oacute;s ativar ou desativar o perfil.

Uma super inst&acirc;ncia gerenciada pode agendar o pr&oacute;prio rein&iacute;cio com:

`python interfaces/telegram/scripts/restart_gateway.py request`

O pedido n&atilde;o encerra diretamente o processo que o executou. O gateway cria um
relan&ccedil;ador externo destacado, para de buscar novas atualiza&ccedil;&otilde;es e
drena o trabalho atual e os itens que j&aacute; estavam na fila. Depois que o PID antigo
termina, o relan&ccedil;ador inicia outra c&oacute;pia, confirma o novo PID e sai.

O estado transit&oacute;rio usa `gateway-restart-request.json` e
`gateway-restart-worker.json` dentro de `state_dir`; o diagn&oacute;stico fica em
`gateway-restart.log`. O relan&ccedil;ador &eacute; criado pelo gateway, e n&atilde;o
pelo processo tempor&aacute;rio do Codex, para sobreviver ao encerramento da &aacute;rvore
do trabalho. Solicita&ccedil;&otilde;es duplicadas s&atilde;o recusadas.

Com `approval_policy = "never"`, um comando que não corresponda às regras públicas é
recusado em vez de solicitar aprovação pela conversa. Ao criar um novo script que a
interface deva executar, adicione seu ponto de entrada ao modelo de regras, sem liberar
o interpretador Python ou o PowerShell de forma genérica.

## Estado e recuperação

```powershell
python interfaces/telegram/gateway.py status
python interfaces/telegram/gateway.py pairing cancel
```

O `update_id` do Telegram é persistido antes do processamento para impedir execução
duplicada. Uma sessão só é substituída depois que o Codex devolve um novo
`thread_id`. Falhas não revelam token, senha mestra nem saída bruta do processo.

Ao reiniciar, jobs que estavam na fila ou executando são marcados como falhos. Um
artefato que estava em `uploading` passa para `unknown`: ele não é reenviado
automaticamente, pois a Bot API pode ter aceitado o upload antes da queda e não oferece
uma chave geral de idempotência para essa operação.
