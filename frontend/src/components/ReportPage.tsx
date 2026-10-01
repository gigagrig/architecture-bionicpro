import React, { useEffect, useState } from 'react';
import { api } from '../api';

type Session = { username: string; csrf: string; roles: string[]; provider: string | null;
  consent_required: boolean; profile_saved: boolean };

const ReportPage: React.FC = () => {
  const [session, setSession] = useState<Session | null>(null);
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [yandex, setYandex] = useState(false);
  const [declined, setDeclined] = useState(false);
  const [message, setMessage] = useState('');
  const load = async () => {
    const response = await api('/auth/session');
    if (response.ok) setSession(await response.json());
    else if (response.status === 401) setSession(null);
    else throw new Error('Не удалось загрузить сессию');
  };
  useEffect(() => {
    (async () => {
      try {
        const config = await api('/auth/config');
        setYandex((await config.json()).yandex_enabled);
        await load();
        if (new URLSearchParams(window.location.search).has('login')) {
          setMessage('Вход не завершён. Попробуйте ещё раз.');
          window.history.replaceState({}, '', '/');
        }
      } catch { setMessage('Сервис недоступен. Обновите страницу.'); }
      finally { setReady(true); }
    })();
  }, []);
  const action = async (path: string, method: string) => {
    setBusy(true); setMessage('');
    try {
      const response = await api(path, { method, headers: { 'X-CSRF-Token': session?.csrf || '' } });
      const body = await response.json();
      if (response.status === 401) { setSession(null); throw new Error('Сессия истекла. Войдите снова.'); }
      if (!response.ok) throw new Error(body.detail || 'Не удалось выполнить запрос');
      if (path === '/auth/logout') {
        setSession(null);
        if (body.sso_logout_pending) setMessage('Вы вышли из приложения. Завершить сессию в службе входа не удалось.');
      } else {
        await load();
        setMessage(path === '/api/protected' ? 'Доступ подтверждён.' : 'Настройки профиля сохранены.');
      }
    } catch (error) { setMessage(error instanceof Error ? error.message : 'Ошибка запроса'); }
    finally { setBusy(false); }
  };
  const button = 'px-4 py-2 bg-blue-700 text-white rounded disabled:opacity-50';
  return <main className="min-h-screen bg-gray-100 flex items-center justify-center p-6">
    <section className="bg-white rounded-xl shadow p-8 max-w-xl w-full space-y-5">
      <h1 className="text-2xl font-bold">BionicPRO</h1>
      {!ready ? <p>Загрузка…</p> : !session ? <>
        <p>Войдите в личный кабинет. Для входа потребуется одноразовый код из приложения-аутентификатора.</p>
        <div className="flex gap-3 flex-wrap">
          <a className={button} href="/auth/login">Войти</a>
          {yandex && <a className={button} href="/auth/login?provider=yandex">Войти через Яндекс ID</a>}
        </div>
      </> : <>
        <p>Вы вошли как <strong>{session.username}</strong>.</p>
        {session.consent_required && !declined && <div className="border rounded p-4 space-y-3">
          <h2 className="font-bold">Сохранить данные Яндекс ID?</h2>
          <p>Разрешить BionicPRO запросить и сохранить идентификатор, логин, имя, фамилию и email из Яндекса
            для вашего профиля? Данные сохраняются в региональной базе. Отказ не ограничивает вход.
            Удалить сохранённый профиль можно здесь в любое время.</p>
          <button className={button} disabled={busy} onClick={() => action('/auth/consent', 'POST')}>Разрешить</button>{' '}
          <button className="underline" disabled={busy} onClick={() => setDeclined(true)}>Не сейчас</button>
        </div>}
        {session.profile_saved && <button className="underline" disabled={busy}
          onClick={() => action('/auth/profile', 'DELETE')}>Удалить сохранённый профиль Яндекса</button>}
        <p className="text-gray-600">Отчёты о работе протеза появятся после подключения сервиса отчётности.</p>
        <div className="flex gap-3 flex-wrap">
          <button className={button} disabled={busy} onClick={() => action('/api/protected', 'GET')}>Проверить доступ</button>
          <button className="underline" disabled={busy} onClick={() => action('/auth/logout', 'POST')}>Выйти</button>
        </div>
      </>}
      {message && <p role="status" className="p-3 bg-gray-100 rounded">{message}</p>}
    </section>
  </main>;
};
export default ReportPage;
