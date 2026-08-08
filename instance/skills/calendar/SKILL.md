---
name: calendar
description: Consultar disponibilidade, calendários e eventos do Google Calendar, além de criar, alterar ou cancelar compromissos e reuniões autorizadas. Usar quando a tarefa mencionar agenda Google, compromissos, reuniões, horários livres, participantes, lembretes ou Google Meet.
---

# Operar Google Calendar

Usar `python skills/calendar/scripts/calendar.py --profile NOME`. Obter OAuth
internamente por `scripts/google_accounts.py`; nunca aceitar nem revelar tokens.

## Inicializar a configuração

Quando algum arquivo estiver ausente, inicializar os dois modelos sem sobrescrever:

```powershell
python scripts/integration_config.py init google
python scripts/integration_config.py init calendar
```

## Executar

1. Listar perfis com `python scripts/google_accounts.py list`.
2. Consultar calendários antes de usar um identificador não conhecido.
3. Usar `events list` ou `freebusy` para leituras.
4. Usar `events instances` para expandir uma série recorrente.
5. Confirmar datas, fuso, calendário, recorrência, lembretes e participantes antes
   de mutações.
6. Criar, alterar ou cancelar somente com autorização explícita e atual.
7. Usar `--send-updates all` somente quando o pedido autorizar notificações.

```powershell
python skills/calendar/scripts/calendar.py --profile pessoal calendars list
python skills/calendar/scripts/calendar.py --profile pessoal events list `
  --time-min "2026-08-01T00:00:00-03:00" `
  --time-max "2026-08-08T00:00:00-03:00"
python skills/calendar/scripts/calendar.py --profile pessoal freebusy `
  --time-min "2026-08-01T09:00:00-03:00" `
  --time-max "2026-08-01T18:00:00-03:00" --calendar-id primary
python skills/calendar/scripts/calendar.py --profile pessoal events create `
  --summary "Reunião" --start "2026-08-01T10:00:00-03:00" `
  --end "2026-08-01T11:00:00-03:00" --reminder "popup:30" --dry-run
python skills/calendar/scripts/calendar.py --profile pessoal events instances `
  --id ID_DA_SERIE --all-pages
```

Eventos de dia inteiro usam `--all-day` e datas `YYYY-MM-DD`; o fim é exclusivo.
Recorrências usam `--recurrence "RRULE:..."`. Usar `--clear-attendees`,
`--clear-recurrence` ou `--clear-reminders` somente em atualizações.
Não repetir automaticamente uma escrita após timeout. Não improvisar endpoints.
Ler [references/api-contracts.md](references/api-contracts.md) quando houver dúvida.
