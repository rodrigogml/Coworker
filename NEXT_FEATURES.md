# Próximas features

Este documento preserva o planejamento das capacidades transversais que devem evoluir
o Coworker depois do núcleo atual. Ele cobre agendamento, scripts privados da
instância, execução do agente e colaboração por grupos e tópicos do Telegram.

> [!IMPORTANT]
> Uma thread do Codex e um tópico do Telegram são objetos diferentes. A integração
> deve manter um mapeamento explícito entre eles; nunca usar um identificador como se
> fosse o outro.

## Decisões confirmadas

- O agendador inicia e termina junto com o gateway da instância.
- O gateway continua sendo o processo supervisor da instância, sem um segundo
  serviço permanente independente.
- Tarefas poderão ser aprovadas pelo canal em que estão sendo tratadas, desde que a
  aprovação esteja dentro das capacidades previamente concedidas.
- O MVP aceitará somente scripts Python.
- Retomada de thread só será permitida para tarefas marcadas explicitamente como
  retomáveis.
- O modo padrão será uma nova thread do Codex por execução.
- A instalação e a administração do grupo continuam sob controle da pessoa
  proprietária.
- Toda tarefa agendada deve informar um título de tópico obrigatório e exclusivo;
  eventos que criem tarefas devem fornecer esse título no próprio evento.
- Com política `task`, o título Telegram é exatamente o título recebido.
- Com política `run`, o título é `<título recebido>: <variação>`; a variação é
  curta, determinística e não substitui o título original.
- Nenhuma tarefa pode ser habilitada enquanto o supergrupo principal não estiver
  vinculado, validado como Fórum, com o bot administrador e com privacidade
  desabilitada.
- A captura integral das mensagens ocorre localmente, mas o contexto só é enviado ao
  agente quando ele é mencionado ou quando recebe uma resposta a uma mensagem sua.

## Limites reais do Telegram

O Bot API não permite que um bot inicie uma conversa privada com uma pessoa que
jamais interagiu com ele. A pessoa precisa abrir o bot ou adicioná-lo a um grupo.
Portanto, o bot não poderá criar sozinho um grupo compartilhado nem adicionar
automaticamente qualquer usuário. O proprietário deverá criar o supergrupo, ativar o
modo fórum, adicionar o bot e conceder somente os direitos necessários.

Em um supergrupo-fórum, o bot pode criar tópicos se for administrador com
`can_manage_topics`. O tópico criado receberá um `message_thread_id`, que deve ser
persistido junto do `chat_id` e do identificador da tarefa. [A documentação do Bot
API](https://core.telegram.org/bots/api) descreve `createForumTopic`, a exigência de
administração e os campos `chat_id`/`message_thread_id`.

O modo de privacidade do grupo muda o que o bot consegue observar. Com privacidade
habilitada, ele recebe somente comandos, menções e respostas dirigidas a ele, sem o
histórico completo da conversa. Como esta arquitetura exige captura local de todas
as mensagens, a privacidade será obrigatoriamente desligada no @BotFather e o bot
deverá permanecer administrador. [A FAQ oficial explica essa diferença]
(https://core.telegram.org/bots/faq).

O modo de tópicos em conversa privada com o bot pode ser habilitado pelo
@BotFather, mas não substitui o supergrupo-fórum quando várias pessoas precisam
compartilhar uma tarefa. [A documentação de fóruns do Telegram](https://core.telegram.org/api/forum)
descreve os dois modelos.

## Objetivos

1. Executar rotinas recorrentes com estado, retries, idempotência e auditoria.
2. Permitir scripts Python privados da instância sem liberar Python genérico ao
   agente ou ao Telegram.
3. Acionar o agente somente quando um filtro ou uma regra permitir.
4. Separar cada assunto em um tópico Telegram e em uma thread Codex correspondente.
5. Permitir que pessoas autorizadas participem de tarefas compartilhadas sem receber
   acesso administrativo à instância.
6. Preservar o isolamento de `CODEX_HOME`, sandbox, cofre, memória e dados privados.

## Retenção configurável

As retenções são independentes e podem ser definidas globalmente, por grupo e por
tarefa. O padrão mínimo do projeto será de 180 dias:

- mensagens brutas do Telegram: 180 dias;
- anexos recebidos e arquivos temporários: 180 dias;
- artefatos produzidos por jobs: 180 dias;
- resumos e checkpoints: 180 dias;
- threads internas do Codex: manter enquanto ativas e arquivar depois, sem apagar
  automaticamente no MVP.

O instalador deverá permitir valores maiores, mas rejeitar valores inferiores a 180
dias. A limpeza deve ser explícita, auditável e limitada aos dados administrados pelo
Coworker; não deve apagar arquivos internos do `CODEX_HOME` sem política específica.

## Arquitetura geral

```text
Gateway da instância
  ├── Telegram adapter
  ├── Scheduler supervisor
  ├── Automation runner
  └── Codex runtime adapter
        ├── script → evento → agente
        └── prompt agendado → agente
```

O scheduler não chamará diretamente a API do Telegram nem iniciará o Codex por
conta própria. Ele criará eventos internos. Os adaptadores existentes do gateway
serão responsáveis por mensagens, threads, progresso e sessões.

## Agendador

### Responsabilidades

- interpretar intervalos, horários, dias da semana e fuso da instância;
- habilitar, desabilitar, pausar e cancelar tarefas;
- aplicar atraso máximo, retry, backoff e limite de concorrência;
- criar uma execução idempotente por tarefa e evento;
- recuperar execuções depois de reinício do gateway;
- finalizar as execuções quando o gateway for encerrado;
- registrar auditoria sem armazenar credenciais.

### Estado privado

O estado deve ficar dentro de:

```text
data/automation/scheduler.sqlite3
```

Entidades previstas:

- `tasks`: definição e política da tarefa;
- `task_runs`: cada tentativa e seu estado;
- `task_events`: eventos recebidos de scripts;
- `task_leases`: exclusão mútua e recuperação;
- `task_outputs`: referências para artefatos e resultados sanitizados;
- `task_audit`: alterações, aprovações e execuções.

Estados mínimos:

```text
draft → approved → enabled → queued → running
                                      ├── waiting-agent
                                      └── succeeded / failed / unknown / cancelled
```

`unknown` nunca deve ser tratado como falha segura para repetir automaticamente.
Primeiro deve haver consulta de recuperação pelo `run_id`, `event_id` ou referência
de integração.

### Supervisão pelo gateway

O gateway inicia o scheduler como componente filho e registra o PID/estado. Ao
encerrar, deve:

1. impedir novas execuções;
2. aguardar uma janela curta para jobs cooperativos;
3. marcar jobs interrompidos como recuperáveis;
4. finalizar processos filhos remanescentes;
5. encerrar o scheduler antes de terminar.

O reinício do gateway deve reidratar apenas tarefas `enabled`, nunca tarefas em
`draft`, `cancelled` ou `disabled`.

## Scripts privados da instância

### Estrutura

```text
data/automation/scripts/<script-id>/
  manifest.toml
  main.py
  tests/
```

O script não será uma nova skill pública e não poderá gravar no repositório.
Skills públicas continuarão sendo os pontos de entrada para integrações externas.

### Manifesto

Cada script deverá declarar:

- `id` e versão;
- ponto de entrada Python;
- formato de entrada e saída;
- timeout e limites de volume;
- diretórios de leitura e escrita;
- integrações e perfis permitidos;
- se pode emitir evento para o agente;
- se pode solicitar notificação;
- necessidade de rede;
- política de retry.

Exemplo conceitual:

```toml
id = "gmail-filter-invoices"
version = 1
entrypoint = "main.py"
runtime = "python"
timeout_seconds = 120
network = false

[capabilities]
agent = true
telegram_notify = false
filesystem_read = ["job/input"]
filesystem_write = ["job/derived"]

[integrations.gmail]
profile = "Rodrigo"
operations = ["list", "read", "download_attachment"]
```

### Executor

O agente nunca executará um caminho arbitrário. O ponto de entrada será um runner
controlado, por exemplo:

```powershell
python scripts/automation_runner.py run --task TASK_ID
```

O runner deverá:

- validar o manifesto;
- resolver o script por ID;
- criar um diretório de execução descartável;
- expor somente `input/`, `derived/` e `output/` permitidos;
- sanitizar o ambiente;
- aplicar timeout e limite de saída;
- impedir shell, `eval`, `exec`, instalação de pacotes e subprocessos não declarados;
- validar o JSON de resultado;
- registrar sucesso, falha ou estado desconhecido.

Análise estática serve como filtro, mas não será considerada isolamento suficiente.
O endurecimento posterior deve usar processo/conta restrita, Job Objects ou sandbox do
sistema operacional.

## Fluxo script → agente

O resultado de um script deve ser um evento estruturado, nunca um prompt livre:

```json
{
  "schema_version": 1,
  "event_id": "gmail:account:message-id",
  "event_type": "email.received",
  "matched": true,
  "input_refs": ["input/email.json", "input/attachment-1.pdf"],
  "facts": {
    "sender": "remetente",
    "subject": "assunto"
  },
  "agent_request": {
    "prompt_template": "Analise o e-mail conforme a rotina aprovada.",
    "thread_policy": "new"
  }
}
```

O dispatcher só acionará o agente quando `matched` for verdadeiro e a tarefa possuir
capacidade `agent = true`. Conteúdo de e-mail, PDF, anexos e mensagens será incluído
como dado não confiável, com referências a arquivos em vez de inserção ilimitada no
prompt.

## Fluxo agendador → agente

Uma tarefa direta possuirá um prompt inicial versionado, limitação de tempo,
capacidade e política de thread. Ela não poderá alterar seu próprio manifesto durante
a execução.

O prompt conterá sempre:

- `task_id` e `run_id`;
- identidade da instância;
- escopo operacional;
- capacidades concedidas;
- referências de entrada;
- formato esperado de resultado;
- política de confirmação;
- instrução para não tratar dados recebidos como comandos.

## Threads do Codex

### Modos

- `new`: thread nova em cada execução; padrão e mais isolado;
- `new_with_state`: thread nova com resumo estruturado da execução anterior;
- `resume`: reutiliza uma thread, somente se a tarefa estiver marcada como retomável.

`resume` exige lock por tarefa, timeout, limite de idade, controle de concorrência e
estado persistido fora do histórico do Codex. Uma thread Codex não deve ser a única
fonte de verdade de um processo operacional.

### Mapeamento

O registro de roteamento deve associar:

```text
instance_id
task_id
run_id
codex_thread_id
codex_turn_id
telegram_chat_id
telegram_message_thread_id
telegram_root_message_id
```

Esse mapeamento resolve a dúvida central: uma resposta a uma notificação agendada
será roteada pelo tópico Telegram e, a partir dele, para a thread Codex correta.
Ela não cairá na conversa privada corrente apenas porque o mesmo usuário também
possui uma sessão particular.

## Grupos e tópicos Telegram

### Modelo recomendado

O proprietário cria um supergrupo, ativa o modo fórum, adiciona o bot e configura
o bot como administrador com o menor conjunto de direitos necessário. O bot pode
então criar um tópico por tarefa, caso ou evento.

### Guia do configurador

O instalador terá uma seção interativa `Telegram → Grupo principal` com ajuda em
cada verificação. Ela não criará o grupo: orientará a pessoa proprietária a criá-lo
manualmente e a corrigir cada pendência.

O diagnóstico deverá verificar, nesta ordem:

1. bot autenticado e token válido;
2. código de vínculo recebido no grupo;
3. `chat_id` vinculado à instância correta;
4. grupo do tipo supergrupo;
5. modo Fórum ativo;
6. bot presente no grupo;
7. bot administrador;
8. permissão `can_manage_topics` concedida;
9. privacidade do bot desligada no @BotFather;
10. owner local associado ao grupo;
11. política de captura e retenção definida;
12. capacidade de criar um tópico de teste e fechá-lo.

Cada falha deve exibir: estado observado, impacto, instrução de correção e
comando/atalho para repetir a verificação. O configurador não deve permitir
habilitar ou agendar tarefas enquanto qualquer requisito obrigatório estiver
pendente.

O resultado deve distinguir `valid`, `invalid`, `pending` e `not_configured`. Uma
falha de notificação em tarefa já existente não apaga a tarefa nem a execução; ela
ativa o fallback descrito abaixo.

Exemplos de tópicos:

- `Tarefa: revisar e-mails de fornecedores`;
- `Evento: comprovante Pix 2026-08-06`;
- `Caso: cadastro de contraparte`;
- `Relatório: fechamento semanal`.

### Título fornecido pela tarefa

O campo `topic_title` é obrigatório na definição da tarefa e deve ser validado
antes da habilitação. Eventos dinâmicos devem transportar o mesmo campo. O valor
deve ser não vazio, ter no máximo 128 caracteres UTF-8 e ser único entre as tarefas
ativas do mesmo grupo, salvo quando a política usar um tópico por execução.

As regras de nome são:

- `task`: usar exatamente `topic_title`, sem prefixo ou alteração;
- `run`: usar `topic_title: variation`, com uma variação curta derivada de
  `run_uid` ou `event_uid`;
- `case`: usar exatamente `topic_title` enquanto o caso estiver ativo.

O UID não substitui o título. Ele aparece apenas no corpo da mensagem raiz, nos
comandos e no banco operacional.

O bot deve reutilizar o tópico da tarefa quando a execução for retomável e criar um
novo tópico quando o evento for independente. O usuário continua livre para fixar,
renomear, fechar ou reorganizar os tópicos.

### Respostas

Quando uma tarefa produzir uma notificação:

1. o gateway localiza ou cria o tópico autorizado;
2. envia uma mensagem raiz com o resumo;
3. registra `chat_id`, `message_thread_id` e `message_id`;
4. associa o tópico à thread Codex;
5. roteia respostas que sejam replies ou mensagens dentro daquele tópico para a
   thread correta;
6. envia a resposta final no mesmo tópico.

Se a tarefa não puder publicar no grupo, o resultado deve ser registrado como
`notification_pending` ou `notification_failed`. O gateway deve enviar a mensagem
ao particular do owner, quando o owner já tiver iniciado o bot, precedida exatamente
por um cabeçalho operacional equivalente a:

```text
Falha ao enviar a mensagem para o tópico [título]:

Motivo: [diagnóstico sanitizado]
```

O corpo original deve ser preservado abaixo do cabeçalho, com anexos referenciados
pelos caminhos seguros do job. O fallback não deve ser encaminhado para a sessão
privada atual do Codex: ele é uma notificação ao owner e conserva `task_uid`,
`run_uid` e `message_thread_id` para posterior reenvio. Se o particular do owner
também estiver indisponível, manter o artefato em `output/` e registrar a falha
para o diagnóstico local.

### Contexto de grupo

O bot deve responder somente quando:

- for mencionado;
- receber uma resposta a uma mensagem sua;
- receber um comando permitido;
- estiver tratando uma resposta dentro de um tópico de tarefa autorizado.

Para fornecer contexto completo, o gateway deve receber e registrar localmente todas
as mensagens do grupo. A privacidade do bot deve estar obrigatoriamente desligada no
@BotFather; se a verificação constatar privacidade habilitada, o grupo fica
inválido para agendamento e novas tarefas não podem ser habilitadas. A captura local
terá retenção limitada, política de descarte e controle de acesso.

Receber todas as mensagens não significa enviar todas ao agente. O gateway só montará
um contexto para o Codex quando:

- o bot for mencionado;
- uma mensagem responder a uma mensagem do bot;
- houver um comando autorizado;
- existir uma aprovação pendente ligada àquele tópico.

Nos demais casos, a mensagem será apenas persistida conforme a retenção e não
acionará uma thread Codex.

O contexto enviado ao Codex deve ser um resumo estruturado, não uma retransmissão de
todas as mensagens em toda resposta. O gateway deve incluir somente:

- mensagens desde o último checkpoint;
- mensagens citadas ou respondidas;
- arquivos relevantes;
- participantes e IDs autorizados;
- resumo persistido do tópico;
- pedido atual.

## Usuários e autorização

O acesso deve ser separado em duas camadas:

1. **participação no grupo**: controlada pelo Telegram e pelo proprietário;
2. **autorização do agente**: controlada por uma allowlist local.

Cada membro deve ser identificado por `chat_id` e `user_id`, nunca por nome ou
username. O registro deve indicar:

- se pode mencionar o bot;
- se pode responder a tarefas;
- se pode aprovar ações;
- se pode criar ou editar tarefas;
- se pode acessar tópicos específicos;
- se pode consultar resultados sensíveis.

Somente o owner pode:

- alterar a allowlist;
- conceder capacidades;
- ativar modo de captura completa do grupo;
- aprovar rede, segredos ou operações financeiras;
- remover o bot ou alterar a política global.

Uma pessoa autorizada a conversar em um tópico não ganha automaticamente acesso ao
cofre, a outras tarefas ou à memória privada da instância.

## Aprovações por canal

Tarefas novas começam em `draft`. Uma aprovação recebida no Telegram deve conter:

- `chat_id`;
- `user_id`;
- `task_id` ou `run_id`;
- resumo da ação;
- capacidades envolvidas;
- validade da aprovação.

O gateway aceita a aprovação apenas se o usuário estiver autorizado para aquela
tarefa e o pedido for uma resposta ou comando no tópico correto. A aprovação não
pode alterar sandbox, backend, `CODEX_HOME` ou permissões globais.

## Comandos planejados

### Controle local

```powershell
python scripts/automation.py list
python scripts/automation.py doctor
python scripts/automation.py validate TASK_ID
python scripts/automation.py history TASK_ID
python scripts/automation.py enable TASK_ID
python scripts/automation.py disable TASK_ID
```

### Telegram

Comandos futuros, sempre limitados ao owner ou aos membros autorizados:

- `/tasks`: listar tarefas visíveis;
- `/task <id>`: consultar estado;
- `/task pause <id>` e `/task resume <id>`;
- `/task approve <run_id>`;
- `/task cancel <run_id>`;
- `/group status`: mostrar o vínculo do grupo;
- `/group members`: mostrar a allowlist sem dados sensíveis;
- `/topic bind <task_id>`: associar tarefa a tópico existente.

Comandos não devem aceitar JSON, SQL, caminho arbitrário, shell ou configuração
Codex livre.

## Fases de implementação

### Fase 1 — scheduler junto do gateway

- supervisor interno do scheduler;
- banco operacional em `data/automation`;
- intervalos e execuções únicas;
- leases, retries e estados;
- encerramento cooperativo com o gateway.

### Fase 2 — runner Python controlado

- manifestos;
- scripts registrados por ID;
- diretórios de job isolados;
- validação de entrada e saída;
- sem rede e sem shell no MVP.

### Fase 3 — dispatcher do agente

- fluxo script → evento → agente;
- fluxo prompt agendado → agente;
- adapter comum para `app-server` e `exec`;
- `new`, `new_with_state` e `resume` condicionado.

### Fase 4 — notificação e tópicos

- fila de eventos de interface;
- supergrupo-fórum configurado pelo owner;
- criação de tópicos pelo bot;
- roteamento por `chat_id` + `message_thread_id`;
- fallback explícito quando o grupo não estiver disponível.

### Fase 5 — contexto e multiusuário

- allowlist por grupo;
- captura limitada de mensagens;
- contexto incremental por checkpoint;
- controle de participantes e arquivos;
- aprovações no próprio tópico.

### Fase 6 — scripts criados pelo agente

- criação em `data/automation/scripts`;
- manifesto gerado e revisado;
- aprovação de capacidades;
- testes locais do script;
- primeira execução em modo dry-run;
- auditoria e rollback.

### Fase 7 — isolamento reforçado

- processo/conta restrita;
- limites reais de CPU, memória e processos;
- rede concedida por capacidade;
- ambientes descartáveis;
- suporte futuro a outros shells somente depois de validar o modelo Python.

## Cenários de aceite

1. O gateway inicia e encerra o scheduler junto com ele.
2. Uma tarefa desabilitada nunca executa.
3. O mesmo `event_id` não gera dois tratamentos.
4. Um script sem correspondência não aciona o agente.
5. Dados de e-mail e anexos não alteram as instruções do agente.
6. Um timeout fica em `unknown` e não é repetido silenciosamente.
7. O modo `resume` é recusado para tarefa não marcada como retomável.
8. Uma notificação agendada aparece no tópico correto.
9. Uma resposta nesse tópico chega à thread Codex correspondente.
10. Uma mensagem em outro tópico não contamina a execução.
11. Um usuário não autorizado não consegue invocar o agente.
12. O agente responde no grupo somente quando mencionado ou respondido.
13. O contexto enviado ao Codex inclui o checkpoint relevante e os arquivos
    autorizados, sem despejar todo o histórico em toda mensagem.
14. O owner consegue conceder e revogar acesso sem alterar o código.
15. O bot não cria grupo nem adiciona pessoas sem a ação humana exigida pelo
    Telegram.
16. O bot cria tópicos somente no supergrupo-fórum em que recebeu
    `can_manage_topics`.
17. Uma tarefa financeira ainda exige autorização específica da operação.
18. Nenhum script acessa arquivos fora das raízes concedidas.
19. O reinício do gateway recupera tarefas sem duplicar efeitos.
20. A falha da notificação não apaga o resultado da execução.
21. Uma tarefa sem `topic_title` é recusada antes de ser habilitada.
22. Uma tarefa `task` usa exatamente o título fornecido.
23. Uma tarefa `run` usa `<topic_title>: <variação>` sem perder o título original.
24. O instalador identifica grupo sem Fórum, sem `can_manage_topics` ou com
    privacidade habilitada e apresenta instruções corretivas.
25. O scheduler não habilita novas tarefas enquanto o grupo estiver inválido.
26. Uma mensagem que falha no tópico é encaminhada ao particular do owner com o
    cabeçalho `Falha ao enviar a mensagem para o tópico [título]:`, seguido de
    uma linha em branco antes do conteúdo.
27. O gateway recebe todas as mensagens para formar contexto, mas só aciona o Codex
    quando há menção, resposta, comando ou aprovação pendente.
28. Privacidade habilitada no @BotFather torna o grupo inelegível para agendamento.

## Questões para a especificação seguinte

- Qual será o tempo de retenção das mensagens de grupo e anexos?
- O owner quer um grupo único para todas as tarefas ou grupos por domínio?
- A primeira versão deve suportar somente um grupo vinculado por instância?
- Quais categorias de resultados podem ser publicadas no grupo sem confirmação?
- O bot deve fechar automaticamente tópicos concluídos ou apenas sugerir o
  fechamento?
- A aprovação no grupo poderá autorizar operação financeira ou apenas iniciar uma
  etapa que ainda exige confirmação específica?

## Próxima etapa

O primeiro núcleo de contratos foi implementado em
`interfaces/telegram/automation.py`, com testes em
`tests/test_telegram_automation.py`. Ele já centraliza validação de `task_uid`,
`topic_title`, políticas `task`/`run`/`case`, regras de `resume`, bloqueio de
tarefas habilitadas sem grupo válido e o cabeçalho exato do fallback ao owner. Ele
ainda não executa scripts, agenda processos nem chama a API Telegram; essas camadas
serão construídas sobre o contrato testado.

O próximo artefato deve ser uma especificação da feature `automation-and-group-routing`,
com contratos para scheduler, runner, eventos, roteamento Telegram e política de
usuários. Depois dela, elaborar o plano técnico e decompor as fases em tarefas
executáveis.
## Estado executável do MVP

O núcleo inicial do scheduler está em `interfaces/telegram/scheduler.py`: tarefas
persistentes em SQLite privado, gatilhos `interval`/`once`/`event`, execução Python sem
shell e validação de caminhos dentro do projeto. O gateway inicia e encerra o scheduler
junto com seu ciclo de vida. A retenção configurável usa 180 dias como mínimo para
mensagens, anexos, artefatos e resumos.
