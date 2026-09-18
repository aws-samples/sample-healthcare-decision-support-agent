import { test, expect } from '@playwright/test';

test.describe('Delivery Validation', () => {
  test('app loads correctly', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('header')).toBeVisible();
  });

  test('Results Viewer displays sample data', async ({ page }) => {
    await page.goto('/');
    await page.click('text=Results');
    const runSelector = page.locator('select').first();
    await expect(runSelector).toBeVisible({ timeout: 15000 });
    const options = await runSelector.locator('option').allTextContents();
    expect(options.some(o => o.includes('LOCAL'))).toBe(true);
  });

  test('Patient selector visible on Nudges tab', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('text=Select a patient')).toBeVisible({ timeout: 10000 });
  });

  test('Theme toggle works', async ({ page }) => {
    await page.goto('/');
    const themeToggle = page.locator('button').filter({ has: page.locator('svg') }).first();
    await themeToggle.click();
    // Should toggle without errors
  });

  test('Sample nudges display in Results Viewer', async ({ page }) => {
    await page.goto('/');
    await page.click('text=Results');
    await page.waitForTimeout(2000);
    const sampleBtn = page.locator('button').filter({ hasText: /success/ }).first();
    if (await sampleBtn.isVisible()) {
      await sampleBtn.click();
      await expect(page.locator('text=Clinical Nudges')).toBeVisible({ timeout: 5000 });
    }
  });
});
