(function () {
    // 1️⃣ CREATE FLOATING CHAT BUTTON
    const chatButton = document.createElement('div');
    chatButton.id = 'alaaudin-ai-trigger';
    chatButton.innerHTML = '💬';
    Object.assign(chatButton.style, {
        position: 'fixed',
        bottom: '20px',
        right: '20px',
        width: '60px',
        height: '60px',
        backgroundColor: '#0a0a0c',
        color: '#ffffff',
        borderRadius: '50%',
        textAlign: 'center',
        lineHeight: '60px',
        fontSize: '28px',
        cursor: 'pointer',
        boxShadow: '0 4px 15px rgba(0,0,0,0.3)',
        zIndex: '999999',
        transition: 'transform 0.3s ease'
    });
    document.body.appendChild(chatButton);

    // 2️⃣ CREATE THE CHAT IFRAME CONTAINER
    const chatContainer = document.createElement('div');
    chatContainer.id = 'alaaudin-ai-container';
    Object.assign(chatContainer.style, {
        position: 'fixed',
        bottom: '90px',
        right: '25px',
        width: '380px',
        height: '550px',
        backgroundColor: '#ffffff',
        borderRadius: '16px',
        boxShadow: '0 8px 30px rgba(0,0,0,0.2)',
        zIndex: '999999',
        display: 'none',
        overflow: 'hidden',
        border: '1px solid #e4e4e7'
    });

    // 3️⃣ INJECT YOUR LIVE VERCEL FRONTEND LINK HERE
    // Replace 'YOUR_VERCEL_FRONTEND_URL' with your actual live chat UI link
    chatContainer.innerHTML = `
        <iframe src="https://YOUR_VERCEL_FRONTEND_URL" style="width:100%; height:100%; border:none;"></iframe>
    `;
    document.body.appendChild(chatContainer);

    // 4️⃣ TOGGLE LOGIC (Open/Close)
    chatButton.onclick = function () {
        if (chatContainer.style.display === 'none') {
            chatContainer.style.display = 'block';
            chatButton.innerHTML = '✖️';
            chatButton.style.transform = 'rotate(90deg)';
        } else {
            chatContainer.style.display = 'none';
            chatButton.innerHTML = '💬';
            chatButton.style.transform = 'rotate(0deg)';
        }
    };
})();
