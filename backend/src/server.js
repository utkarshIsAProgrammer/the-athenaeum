import express from "express";
import "dotenv/config";
import { config as higsfieldConfig } from "@higgsfield/client/v2";
import cors from "cors";
if (process.env.HF_CREDENTIAL?.trim()) {
    higsfieldConfig({ credentials: process.env.HF_CREDENTIAL.trim() });
}
const app = express();
const PORT = process.env.PORT;
app.use(cors({ origin: "" }));
app.use(express.json());
app.get("/api/health", (req, res) => {
    res.json({ ok: true, service: "genesie-api" });
});
app.listen(PORT, () => {
    console.log(`Server is running on PORT: ${PORT}`);
});
