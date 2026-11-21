import { CheckField } from "./Form";

// HTTP connectors' "Verify TLS certificate" switch, shared by the
// add and edit forms. Off is for self-signed endpoints.
export default function VerifyTlsField({
  id,
  checked,
  onChange,
}: {
  id: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <CheckField
      id={id}
      label="Verify TLS certificate"
      checked={checked}
      onChange={(e) => onChange(e.target.checked)}
      help={checked ? undefined : "For self-signed endpoints."}
    />
  );
}
