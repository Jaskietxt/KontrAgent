# KontrAgent

Веб-интерфейс для генератора PDF-досье из `dossier_report.py`. Сайт не открыт всему интернету: веб-порт контейнера привязан только к `127.0.0.1`, а удалённый доступ настраивается через Tailscale Serve. Для входа дополнительно требуется пароль приложения.

## Что нужно

- Linux-сервер с Docker Compose. Бесплатный ориентир — [Oracle Cloud Free Tier VM](https://www.oracle.com/cloud/free/); доступность бесплатных ресурсов зависит от региона и наличия мощностей. Oracle может запросить платёжную карту при регистрации, а неактивный аккаунт может быть приостановлен. Используйте только ресурсы с пометкой Always Free и проверяйте условия Oracle.
- Tailscale на сервере и устройствах, с которых вы будете заходить. На [странице тарифов Tailscale](https://tailscale.com/pricing) сейчас указан бесплатный Personal для 6 пользователей; условия могут измениться.
- Локальные файлы `rusprofile_cookies.json` и `ai_config.json`. Они содержат секреты и не включаются в Git или Docker-образ.

Сайт должен оставаться запущенным на VM. Oracle Free Tier не является гарантией доступности или SLA.

## Быстрый локальный запуск на Windows

Это проверит сайт на вашем компьютере, но не сделает его доступным удалённо.

1. Установите и запустите Docker Desktop для Windows. Убедитесь, что в репозитории присутствуют `rusprofile_cookies.json` и `ai_config.json`; не отправляйте их содержимое в чат или GitHub.
2. В PowerShell из папки проекта создайте `.env` с новым паролем:

   ```powershell
   $bytes = New-Object byte[] 32
   $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
   $rng.GetBytes($bytes)
   $password = -join ($bytes | ForEach-Object { $_.ToString('x2') })
   Set-Content -Path .env -Value "APP_PASSWORD=$password`nSESSION_COOKIE_SECURE=false"
   Write-Output "Имя: kontragent`nПароль: $password"
   $rng.Dispose()
   ```

   `SESSION_COOKIE_SECURE=false` разрешён только для локальной проверки на `http://127.0.0.1`. Не используйте эту настройку для удалённого сайта.

3. Соберите и запустите сайт:

   ```powershell
   docker compose up -d --build
   docker compose logs -f web
   ```

4. Откройте [http://127.0.0.1:8000](http://127.0.0.1:8000), войдите с пользователем `kontragent` и сохранённым выше паролем. Для проверки генерации PDF введите ИНН/ОГРН. Нужны действующие cookies; включённый AI использует настроенный провайдер и может передавать ему данные отчёта/взимать плату.
5. Остановите сайт командой `docker compose down`.

## Подготовка сервера

1. Создайте Linux VM в Oracle Cloud и установите Docker Engine и Docker Compose Plugin по [официальной инструкции Docker для Ubuntu](https://docs.docker.com/engine/install/ubuntu/).
2. Получите исходный код на VM. Не переносите секреты через Git. Передайте файлы `rusprofile_cookies.json` и `ai_config.json` на сервер отдельным защищённым способом и ограничьте права доступа:

   ```sh
   chmod 600 rusprofile_cookies.json ai_config.json
   ```

3. Создайте `.env` рядом с `docker-compose.yml`, сгенерировав отдельный длинный пароль:

   ```sh
   APP_PASSWORD="$(openssl rand -hex 32)"
   printf 'APP_PASSWORD=%s\nSESSION_COOKIE_SECURE=true\n' "$APP_PASSWORD" > .env
   printf 'Сохраните пароль для входа: %s\n' "$APP_PASSWORD"
   unset APP_PASSWORD
   chmod 600 .env
   ```

   Пароль также понадобится для входа на сайт. Имя пользователя — `kontragent`.

4. Соберите и запустите приложение:

   ```sh
   docker compose up -d --build
   docker compose logs -f web
   ```

Контейнер использует AI-конфигурацию как раньше: если в `ai_config.json` AI включён, генератор отправит необходимые данные настроенному AI-провайдеру. Это может иметь отдельные условия, ограничения и стоимость. Сайт показывает это предупреждение перед формированием отчёта.

## Приватный удалённый доступ через Tailscale

1. Установите [Tailscale на VM](https://tailscale.com/kb/1031/install-linux) и выполните вход в тот же аккаунт/tailnet, который будет использоваться на ваших устройствах.
2. Установите Tailscale на личные устройства и войдите в тот же tailnet. В настройках tailnet включите HTTPS certificates, если Tailscale предложит это для Serve.
3. На VM запустите [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve):

   ```sh
   sudo tailscale serve --bg 8000
   sudo tailscale serve status
   ```

4. Откройте выданный адрес `https://…ts.net` на устройстве с подключённым Tailscale. Введите имя `kontragent` и пароль из `.env`.

Не включайте Tailscale Funnel и не открывайте порт `8000` в интернет/правилах Oracle. Compose публикует приложение только на loopback, а Tailscale Serve проксирует его внутри tailnet. В правилах облачной сети оставьте только необходимый SSH-доступ и не добавляйте публичные правила HTTP/HTTPS.

После перезапуска VM Docker Compose и Tailscale Serve должны восстановить доступ автоматически. Проверяйте состояние командами `docker compose ps` и `sudo tailscale serve status`.

## Важное о ключах и cookies

Конфигурация AI и cookies Rusprofile ранее хранились в Git. Удаление файлов из текущей версии не удаляет уже созданные коммиты. Отзовите/обновите Rusprofile cookies и перевыпустите API-ключ AI у провайдера, если он активен. Не публикуйте эти файлы и не добавляйте их в коммиты.
