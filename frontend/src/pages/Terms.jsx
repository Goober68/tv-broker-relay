export default function TermsPage() {
  return (
    <div className="max-w-3xl mx-auto py-12 px-6 animate-fade-in">
      <h1 className="font-display font-bold text-2xl text-base-50 mb-2">Terms of Service</h1>
      <p className="text-xs text-base-500 mb-8">Last updated: March 28, 2026</p>

      <div className="prose-policy space-y-6 text-sm text-base-300 leading-relaxed">
        <section>
          <h2>1. Service Description</h2>
          <p>
            TV Broker Relay ("the Service") is a webhook relay that receives alerts from
            TradingView and forwards them as trade orders to supported broker APIs. The
            Service does not provide financial advice, trade signals, or investment
            recommendations.
          </p>
        </section>

        <section>
          <h2>2. Eligibility</h2>
          <p>
            You must be at least 18 years old and legally permitted to trade financial
            instruments in your jurisdiction. You are responsible for ensuring your use of
            the Service complies with all applicable laws and your broker's terms of service.
          </p>
        </section>

        <section>
          <h2>3. Account Responsibility</h2>
          <p>
            You are solely responsible for your account credentials, broker API keys, and
            any orders submitted through the Service. You must keep your credentials secure
            and notify us immediately if you suspect unauthorized access.
          </p>
        </section>

        <section>
          <h2>4. No Guarantee of Execution</h2>
          <p>
            The Service relays orders to broker APIs on a best-effort basis. We do not
            guarantee order execution, fill prices, or that the Service will be available
            without interruption. Network latency, broker API outages, rate limits, and
            other factors outside our control may affect order delivery and execution.
          </p>
        </section>

        <section>
          <h2>5. Risk Disclaimer</h2>
          <p>
            <strong>Trading financial instruments involves substantial risk of loss and is
            not suitable for all investors.</strong> You may lose more than your initial
            investment. Past performance is not indicative of future results. The Service
            is a tool — all trading decisions and their consequences are yours alone.
          </p>
        </section>

        <section>
          <h2>6. Limitation of Liability</h2>
          <p>
            To the maximum extent permitted by law, TV Broker Relay and its operators shall
            not be liable for any direct, indirect, incidental, consequential, or punitive
            damages arising from your use of the Service, including but not limited to
            trading losses, missed trades, duplicate orders, incorrect order parameters, or
            service downtime.
          </p>
        </section>

        <section>
          <h2>7. Subscription and Billing</h2>
          <p>
            Paid plans are billed monthly via Stripe. You may cancel at any time; access
            continues through the end of the billing period. Refunds are handled on a
            case-by-case basis. Free plan users may be subject to usage limits as described
            on the billing page.
          </p>
        </section>

        <section>
          <h2>8. Acceptable Use</h2>
          <p>You agree not to:</p>
          <ul>
            <li>Use the Service to circumvent broker rules or regulatory requirements</li>
            <li>Attempt to reverse-engineer, exploit, or attack the Service infrastructure</li>
            <li>Share your account or API keys with unauthorized parties</li>
            <li>Submit webhooks at a rate that exceeds your plan's limits</li>
          </ul>
        </section>

        <section>
          <h2>9. Termination</h2>
          <p>
            We reserve the right to suspend or terminate accounts that violate these terms
            or that we reasonably believe are being used for abusive or fraudulent purposes.
            You may delete your account at any time.
          </p>
        </section>

        <section>
          <h2>10. Changes to Terms</h2>
          <p>
            We may update these terms from time to time. Continued use of the Service after
            changes constitutes acceptance of the updated terms. Material changes will be
            communicated via email or in-app notification.
          </p>
        </section>

        <section>
          <h2>11. Contact</h2>
          <p>
            For questions about these terms, email{' '}
            <a href="mailto:support@tvbrokerrelay.com" className="text-accent hover:underline">
              support@tvbrokerrelay.com
            </a>.
          </p>
        </section>
      </div>
    </div>
  )
}
