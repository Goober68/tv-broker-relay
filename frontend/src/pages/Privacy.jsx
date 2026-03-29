export default function PrivacyPage() {
  return (
    <div className="max-w-3xl mx-auto py-12 px-6 animate-fade-in">
      <h1 className="font-display font-bold text-2xl text-base-50 mb-2">Privacy Policy</h1>
      <p className="text-xs text-base-500 mb-8">Last updated: March 28, 2026</p>

      <div className="prose-policy space-y-6 text-sm text-base-300 leading-relaxed">
        <section>
          <h2>1. What We Collect</h2>
          <p>
            When you create an account we store your <strong>email address</strong> and a
            bcrypt-hashed password. We never store your password in plain text.
          </p>
          <p>
            When you connect a broker account we store <strong>encrypted API credentials</strong>
            (AES-256 Fernet encryption at rest) so the relay can submit orders on your behalf.
            We do not store broker account passwords in plain text.
          </p>
          <p>
            We log <strong>webhook deliveries</strong> (source IP, payload minus secrets,
            broker request/response) for debugging and audit purposes. Delivery logs are
            automatically purged after 30 days.
          </p>
        </section>

        <section>
          <h2>2. How We Use Your Data</h2>
          <ul>
            <li>Relay TradingView webhook alerts to your connected broker accounts</li>
            <li>Display order history, positions, and P&L in the dashboard</li>
            <li>Send optional email notifications (daily P&L summary, order alerts)</li>
            <li>Process subscription payments via Stripe</li>
          </ul>
        </section>

        <section>
          <h2>3. Third-Party Services</h2>
          <p>We integrate with the following third parties:</p>
          <ul>
            <li><strong>Broker APIs</strong> (Tradovate, Oanda, IBKR, etc.) — to execute trades and fetch account data</li>
            <li><strong>Tradovate OAuth</strong> — for secure account authorization without sharing passwords with us</li>
            <li><strong>Stripe</strong> — for payment processing. We do not store credit card numbers; Stripe handles all payment data</li>
          </ul>
          <p>We do not sell, share, or provide your personal data to any other third parties.</p>
        </section>

        <section>
          <h2>4. Data Security</h2>
          <ul>
            <li>All traffic is encrypted via TLS (HTTPS)</li>
            <li>Broker credentials are AES-256 encrypted at rest</li>
            <li>JWT access tokens expire after 15 minutes</li>
            <li>Refresh tokens are stored in httpOnly secure cookies</li>
            <li>The application runs on a private network with no direct database access from the internet</li>
          </ul>
        </section>

        <section>
          <h2>5. Data Retention</h2>
          <p>
            Order history and position data are retained for the lifetime of your account.
            Webhook delivery logs are purged after 30 days. You may request deletion of your
            account and all associated data by contacting us.
          </p>
        </section>

        <section>
          <h2>6. Cookies</h2>
          <p>
            We use a single httpOnly cookie for session management (refresh token).
            We do not use tracking cookies, analytics scripts, or advertising pixels.
          </p>
        </section>

        <section>
          <h2>7. Your Rights</h2>
          <p>
            You may request access to, correction of, or deletion of your personal data
            at any time by contacting us at the email below.
          </p>
        </section>

        <section>
          <h2>8. Contact</h2>
          <p>
            For privacy-related inquiries, email{' '}
            <a href="mailto:support@tvbrokerrelay.com" className="text-accent hover:underline">
              support@tvbrokerrelay.com
            </a>.
          </p>
        </section>
      </div>
    </div>
  )
}
