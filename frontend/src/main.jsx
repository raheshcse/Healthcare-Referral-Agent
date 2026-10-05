import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "./index.css";
import ClinicianApp from "./clinician/ClinicianApp.jsx";
document.title = "Healthcare Referral Agent";

createRoot(document.getElementById("root")).render(
  <StrictMode><ClinicianApp /></StrictMode>,
);
