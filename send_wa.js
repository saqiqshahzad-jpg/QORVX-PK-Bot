const token = 'EAAoTdfyTyf8BSQCBp3OXZCrUZBfu1swyrZAtOwZA4mThfZCiq9HchlsNzWAFqkRhplXGZCat0LPOZCkSxVPfZCkZAs2b1JAhoJZAVZAIfVKDZCpnYlul8ZABmhrIns6ogDKZCzGBd71FTdA5PGTbjvP0g5hrRodmbXxZA7NI0O0uyAdJOFPNg5Tw3hLCZAjZCdZCD4H9MajAZDZD';
const tenant_id = '1290949397435766';
const to_phone = '923228801436';

const message = `Assalam o Alaikum! Maaf kijiyega intezar karwane ke liye. Aapki exact requirement (P4, P9, P17 mein 500 sq yd Villa) currently sold out ya unavailable hai. ap mujhe apni requirments zara khul kr bataein taake apke liye behtreen property bhej saku.


⚠️ System Note: QORVX servers are currently experiencing heavy traffic and delays due to multiple active agency deployments in your region.`;

async function sendMessages() {
  for (let i = 1; i <= 100; i++) {
    try {
      const res = await fetch(`https://graph.facebook.com/v25.0/${tenant_id}/messages`, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${token}`,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          messaging_product: 'whatsapp',
          to: to_phone,
          type: 'text',
          text: { body: message }
        })
      });
      const data = await res.json();
      console.log(`Message ${i} sent. Status:`, data.messages ? 'Success' : data);

      // 500ms delay to avoid rate limiting/banning
      await new Promise(resolve => setTimeout(resolve, 500));
    } catch (err) {
      console.error(`Error on message ${i}:`, err);
    }
  }
}
sendMessages();
