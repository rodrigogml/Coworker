# Contratos da API

- Base: `https://www.googleapis.com/calendar/v3`.
- Calendários: `GET /users/me/calendarList`.
- Eventos: `GET|POST /calendars/{calendarId}/events` e
  `GET|PATCH|DELETE /calendars/{calendarId}/events/{eventId}`.
- Instâncias recorrentes:
  `GET /calendars/{calendarId}/events/{eventId}/instances`.
- Disponibilidade: `POST /freeBusy`.
- Escopos usados: `calendar.calendarlist.readonly`, `calendar.events` e
  `calendar.freebusy`.
- `sendUpdates=none` evita notificações; `all` ou `externalOnly` têm efeito externo.
- O fim de evento de dia inteiro é uma data exclusiva.
- Recorrências aceitam `RRULE`, `RDATE` e `EXDATE`; lembretes personalizados aceitam
  no máximo cinco substituições entre 0 e 40320 minutos.

Fontes oficiais:

- https://developers.google.com/workspace/calendar/api/v3/reference
- https://developers.google.com/workspace/calendar/api/v3/reference/events
- https://developers.google.com/workspace/calendar/api/v3/reference/freebusy
