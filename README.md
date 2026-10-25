# KontrAgent

Веб-интерфейс для генератора PDF-досье из `dossier_report.py`: поиск, сохранённые проверки, факторы риска, полнота данных и выгрузка PDF. Приложение требует пароль. Публичный HTTPS-доступ включается отдельным профилем Docker Compose с Caddy; сам веб-порт остаётся привязан к `127.0.0.1`. Все вошедшие пользователи видят общую историю сохранённых проверок.

## Что нужно

- Для публичного сайта: Linux-сервер/VM с Docker Compose и публичным IP. Бесплатный ориентир — [Oracle Cloud Free Tier VM](https://www.oracle.com/cloud/free/); доступность бесплатных ресурсов зависит от региона и наличия мощностей. Oracle может запросить платёжную карту при регистрации, а неактивный аккаунт может быть приостановлен. Используйте только ресурсы с пометкой Always Free и проверяйте условия Oracle.
- Для публичного сайта: собственный домен с DNS-записью на сервер. Caddy автоматически получает TLS-сертификат; для этого сервер должен быть доступен по TCP-портам 80 и 443.
- Локальные файлы `rusprofile_cookies.json` и `ai_config.json`. Они содержат секреты и не включаются в Git или Docker-образ.

Сайт доступен из интернета, но требует пароль приложения. VM должна оставаться запущенной; Oracle Free Tier не является гарантией доступности или SLA.

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

4. Откройте [http://127.0.0.1:8000](http://127.0.0.1:8000), войдите с пользователем `kontragent` и сохранённым выше паролем. Для проверки генерации PDF введите ИНН/ОГРН. Новые проверки и их PDF/HTML сохраняются в Docker volume; открыть досье и скачать PDF можно из карточки организации. Нужны действующие cookies; если AI включён, данные отчёта могут передаваться настроенному провайдеру.
5. Остановите сайт командой `docker compose down`.

При `docker compose down` история сохраняется в именованном Docker volume. Удалять её можно командой `docker compose down -v` — это безвозвратно удалит сохранённые отчёты.

## Публичное размещение с HTTPS и паролем

1. Создайте Linux VM (например, в Oracle Cloud) и установите Docker Engine и Docker Compose Plugin по [официальной инструкции Docker для Ubuntu](https://docs.docker.com/engine/install/ubuntu/).
2. Создайте DNS-запись типа `A` для поддомена (например, `dossier.example.com`) с публичным IPv4-адресом VM. Если у VM нет настроенного IPv6, не добавляйте запись `AAAA`.
3. В правилах облачного и системного firewall разрешите входящие TCP-порты `80` и `443` (UDP `443` необязателен, нужен только для HTTP/3). По возможности ограничьте SSH своим IP. Не открывайте порт `8000`: он привязан только к loopback.
4. Получите исходный код на VM. Не переносите секреты через Git. Передайте файлы `rusprofile_cookies.json` и `ai_config.json` на сервер отдельным защищённым способом и ограничьте права доступа:

   ```sh
   chmod 600 rusprofile_cookies.json ai_config.json
   ```

   Перед публичным запуском обязательно отзовите старые cookies Rusprofile и перевыпустите активный API-ключ AI: эти секреты раньше находились в истории Git, и простое удаление файлов не отзывает их.

5. Создайте `.env` рядом с `docker-compose.yml`. Укажите домен и сгенерируйте отдельный длинный пароль:

   ```sh
   DOMAIN="dossier.example.com"
   APP_PASSWORD="$(openssl rand -hex 32)"
   printf 'DOMAIN=%s\nAPP_PASSWORD=%s\nSESSION_COOKIE_SECURE=true\n' "$DOMAIN" "$APP_PASSWORD" > .env
   printf 'Сохраните пароль для входа: %s\n' "$APP_PASSWORD"
   unset APP_PASSWORD DOMAIN
   chmod 600 .env
   ```

   Сохраните пароль в менеджере паролей. Имя пользователя — `kontragent`. Все, кому вы передадите пароль, смогут читать общую историю и создавать отчёты.

6. Соберите и запустите приложение вместе с публичным HTTPS-прокси:

   ```sh
   docker compose --profile public up -d --build
   docker compose ps
   docker compose logs -f web caddy
   ```

   Когда DNS обновится и Caddy получит TLS-сертификат, откройте `https://dossier.example.com`. Браузер запросит имя `kontragent` и пароль из `.env`. Не вводите пароль на HTTP-странице. Если Caddy не получает сертификат, проверьте DNS и доступность портов 80/443.

Обновление сайта: `git pull`, затем `docker compose --profile public up -d --build`. Отчёты и сертификаты сохраняются в Docker volumes. Резервно копируйте важные данные; `docker compose down -v` безвозвратно удалит отчёты и сохранённые сертификаты. Не открывайте порт `8000` в облачном firewall и не публикуйте `.env`, `rusprofile_cookies.json`, `ai_config.json` или Docker volume с отчётами.

Если в `ai_config.json` AI включён, данные отчёта могут передаваться настроенному AI-провайдеру и обрабатываться по его условиям, ограничениям и тарифам. Сайт показывает предупреждение перед формированием отчёта.

## Приватный доступ через Tailscale

Если сайт должен быть доступен только вам, не включайте публичный профиль и не открывайте порты 80/443. Установите Tailscale на VM и устройства и войдите в один tailnet. Затем запустите [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve):

```sh
sudo tailscale serve --bg 8000
sudo tailscale serve status
```

Откройте выданный адрес `https://…ts.net` на устройстве с подключённым Tailscale и введите имя `kontragent` и пароль из `.env`.

Не включайте Tailscale Funnel. После перезапуска VM проверяйте состояние командой `docker compose ps` и `sudo tailscale serve status`.

## Важное о ключах и cookies

Конфигурация AI и cookies Rusprofile ранее хранились в Git. Удаление файлов из текущей версии не удаляет уже созданные коммиты. Отзовите/обновите Rusprofile cookies и перевыпустите API-ключ AI у провайдера, если он активен. Не публикуйте эти файлы и не добавляйте их в коммиты.
