# TorAPI через локальный SOCKS5 — проверка 13 ноября 2026 года

Локальная копия [TorAPI](https://github.com/Lifailon/TorAPI) (`c1d995d`, версия 0.5.2) находится вне MusicStream: `%TEMP%\MusicStream-TorAPI-local`. Зависимости установлены только в эту копию. MusicStream, Source Manager и BitTorrent-плеер не менялись.

## Изменения в локальной копии

- В `main.js` вместо `https-proxy-agent` используется `socks-proxy-agent@8.0.5` с `socks5h://127.0.0.1:10808` и `axios` `proxy: false`. Суффикс `h` отправляет доменное имя прокси для удалённого DNS. TLS-проверка не отключена.
- Из активных заголовков RuTracker и Kinozal, а также отключённого пути Puppeteer удалены встроенные чужие cookies. В исходнике есть несколько адресов зеркал; их автоматический перебор сохранён. Собственная авторизация RuTracker не добавлялась.
- `web.listen(port, '127.0.0.1')` ограничивает HTTP-сервер loopback. Проверка `netstat` показала только `127.0.0.1:18743 LISTENING`.
- Запуск из каталога копии: `node main.js --port 18743`. Перед запуском нужен активный HAPP/Xray SOCKS5 на `127.0.0.1:10808`. После проверки сервер остановлен.

## Практические результаты

| Проверка | Результат |
| --- | --- |
| Node Axios + SOCKS5H, `GET https://rutracker.org/`, без cookie | `301` на `/forum/index.php` |
| `HEAD https://rutracker.org/forum/index.php`, без cookie | `200` |
| `GET https://rutracker.org/forum/tracker.php`, без cookie | `403` |
| `HEAD https://feed.rutracker.cc/atom/f/428.atom`, без cookie | `200`, `application/atom+xml` |
| Локальный `GET /api/get/category/rutracker` | `200` |
| Один поиск `Daft Punk Random Access Memories`, категория 0, страница 0 | TorAPI ответил HTTP `200` с `{"Result":"Server is not available"}`; реальных раздач нет |

В журнале TorAPI первые два обращения к зеркалам завершились ошибкой без кода, третье — `UNABLE_TO_GET_ISSUER_CERT_LOCALLY`. Сертификат не обходили. Истинное правило авторизации и допустимость автоматического поиска требуют подтверждения у источника; `403` фиксирует техническую недоступность гостевого поиска в этой проверке.

На момент первой проверки `parse_search()` в `TorApiDiscoveryProvider` трактовал объект `{"Result": ...}` как пустой список и скрывал ошибку источника от вызывающего кода. Совместимость с **реальным успешным** JSON-ответом не проверена. Поиск по ID не выполнялся, `.torrent`, magnet-метаданные и аудиофайлы не скачивались.

## Уточнение причины 403 и исправление адаптера

Повторная ограниченная диагностика 13 ноября показала: гостевой `GET /forum/index.php` возвращает `200` и ссылки на «Трекер»/«Поиск», а `GET /forum/tracker.php` возвращает `403` с `server: cloudflare`, `cf-mitigated: challenge`, заголовком HTML `Just a moment...` и требованием JavaScript/cookies. `GET /forum/faq.php` также встречает challenge. Это **проверка Cloudflare до страницы поиска**, а не доказательство обязательной авторизации RuTracker. CAPTCHA, браузерные cookies и токены проверки не использовались.

Доступный [robots.txt RuTracker](https://rutracker.org/robots.txt) объявляет для `User-agent: *` `Allow: /` и `Content-Signal: search=yes,ai-train=no,use=reference`. Это сигнал об индексации, но не гарантия доступа к защищённому `tracker.php` и не замена правилам использования сайта. FAQ и условия автоматизированного поиска не удалось прочитать из-за challenge. [Исходный TorAPI](https://github.com/Lifailon/TorAPI/blob/main/main.js) не поддерживает собственную пользовательскую сессию настройкой: `username`/`password` используются только для прокси, а cookie RuTracker зашит в коде. Для личной сессии потребовалась бы отдельная, разрешённая источником реализация; копирование cookies из браузера автоматически не выполнялось.

Один гостевой `GET https://feed.rutracker.cc/atom/f/428.atom` через SOCKS5H вернул `200`, 36 записей с полями `title`, `author`, `link`, `id`, `updated`, `category`. Совпадений по «Daft Punk Random Access Memories» в текущем выпуске не было. Лента позволяет локально фильтровать **новые** записи категории, но не искать по всему каталогу, не содержит полного списка файлов и не подтверждает разрешение на массовое индексирование.

В MusicStream `TorApiDiscoveryProvider` теперь выбрасывает отдельные исключения для сетевой недоступности, HTTP `401/403`, серверной ошибки и ответа `{"Result":"Server is not available"}`. Только явное сообщение `No matches were found for your title` считается пустой выдачей. Локальные тесты адаптера: 10 прошли. Успешный поиск RuTracker через TorAPI по-прежнему не подтверждён.

## Экспериментальная личная авторизация в локальной копии

В `%TEMP%\MusicStream-TorAPI-local` добавлен модуль `rutracker-auth.js` и `tough-cookie@5`. Модуль использует HTTPS через `socks5h://127.0.0.1:10808` с обычной проверкой TLS, принимает cookies только от `rutracker.org` и держит их в памяти `CookieJar`. Внешний заголовок `Cookie` отвергается, редиректы не пересылаются автоматически. Пароль вводится без отображения в локальном терминале только после предварительной проверки страницы входа; в файлы, URL, параметры процесса и логи он не записывается. Защищённое постоянное хранение сессии не добавлено.

Гостевой `GET /forum/index.php` вернул штатную POST-форму `login.php` с полями `login_username`, `login_password` и `login` (плюс возможные скрытые поля). `GET /forum/login.php` вернул `403` с `cf-mitigated: challenge`, поэтому проверка `node main.js --rutrackerLogin --port 18744` остановилась с `CHALLENGE` **до запроса учётных данных и до POST**. Живая авторизация и собственная сессия не установлены. Один поисковый запрос после входа не выполнялся. Без входа локальный маршрут поиска теперь возвращает HTTP `401` и `{"Result":"AUTH_REQUIRED"}` без обращения к RuTracker.

Модуль различает `AUTH_REQUIRED`, `AUTH_FAILED`, `FORBIDDEN`, `CHALLENGE`, `NETWORK` и ошибку источника. Три офлайн-теста проверили форму, остановку на challenge и синтетическую сессию только в памяти; все прошли. Прохождение Cloudflare программным запросом не подтверждено. Даже успешный вход через браузер пользователя не доказывает доступность отдельного Node.js-сеанса: копирование браузерных cookies и обход интерактивной проверки не выполнялись.

## Дальнейшее решение

Повторять анонимный поиск или обходить `403` не следует. При наличии подтверждённого правилами способа использовать личную учётную запись потребуется отдельная авторизация и безопасное хранение пользовательской сессии; `USERNAME`/`PASSWORD` TorAPI относятся к прокси, не к RuTracker. Как ограниченную альтернативу можно изучить официальный Atom-канал категории и локально фильтровать заголовки по исполнителю и альбому, предварительно проверив условия его использования. Для полной интеграции также нужно различать ошибку TorAPI и пустую выдачу в адаптере.

Источники: [TorAPI main.js](https://github.com/Lifailon/TorAPI/blob/main/main.js), [package.json](https://github.com/Lifailon/TorAPI/blob/main/package.json), [README](https://github.com/Lifailon/TorAPI/blob/main/README.md), [socks-proxy-agent](https://github.com/TooTallNate/proxy-agents/tree/main/packages/socks-proxy-agent).
