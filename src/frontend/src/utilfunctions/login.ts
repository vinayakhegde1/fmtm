import { createPopup } from './createPopup';

export const createLoginWindow = (redirectTo) => {
  fetch(`${import.meta.env.VITE_API_URL}/auth/osm_login/`)
    .then((resp) => resp.json())
    .then((resp) => {
      const popup = createPopup('OSM auth', resp.login_url);
      if (!popup) {
        console.warn('Popup blocked or unavailable.');
        return;
      }

      // Get OAuth2 authorization url, extract state
      const authUrl = new URL(resp.login_url);
      const authParams = new URLSearchParams(authUrl.search);
      const responseState = authParams.get('state');

      const handleLoginPopup = (event) => {
        // OAuth2 state param validation
        if (event.data.authCode && event.data.state === responseState) {
          // Clean up the postMessage event listener
          window.removeEventListener('message', handleLoginPopup);

          const authCode = event.data.authCode;
          const state = event.data.state;
          const callback_url = `${import.meta.env.VITE_API_URL}/auth/callback/?code=${authCode}&state=${state}`;

          fetch(callback_url, { credentials: 'include' })
            .then((resp) => resp.json())
            .then((res) => {
              fetch(`${import.meta.env.VITE_API_URL}/auth/me/`, {
                credentials: 'include',
              })
                .then((resp) => resp.json())
                .then((userRes) => {
                  const params = new URLSearchParams({
                    username: userRes.user_data.username,
                    osm_oauth_token: res.access_token,
                    id: userRes.user_data.id,
                    picture: userRes.user_data.img_url,
                    redirect_to: redirectTo,
                  }).toString();
                  const redirectUrl = `/osmauth?${params}`;
                  window.location.href = redirectUrl;
                });
            });
        } else {
          console.log('States do not match');
        }
      };

      // Add the postMessage event listener
      window.addEventListener('message', handleLoginPopup);
    });
};

export const revokeCookie = async () => {
  try {
    const response = await fetch(`${import.meta.env.VITE_API_URL}/auth/logout/`, { credentials: 'include' });
    if (!response.ok) {
      console.error('/auth/logout endpoint did not return 200 response');
    }
  } catch (error) {
    throw error;
  }
};
